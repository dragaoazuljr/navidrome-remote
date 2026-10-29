"""mpv JSON-IPC client (AF_UNIX datagram socket).

Speaks the stock mpv IPC protocol: newline-delimited JSON requests with a
`request_id`, correlated responses, unsolicited `event` objects drained to
an observer hook. The SAME code talks to:

  * real mpv on the SBC (--input-ipc-server=<socket>), and
  * tools/fake_mpv_sink.py on the dev-workspace (C1, --null-audio/mock).

That symmetry is what makes the service bed-agnostic: the socket path is
env-driven, the protocol is constant.

The socket ops are blocking (control plane: a few dozen packets/min) and are
expected to be invoked via `asyncio.to_thread` from async callers, or called
directly from synchronous contexts.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time


class EngineDown(Exception):
    """IPC socket unreachable — engine (mpv or mock) is not running."""


class MpvEngine:
    PROTOCOL_VERSION_NOTE = "mpv JSON-IPC (newline JSON, AF_UNIX dgram)"

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._sock: "socket.socket | None" = None
        self._req_counter = 0
        self._lock = threading.Lock()
        self.last_contact: "float | None" = None
        self.event_observer = None  # callable(dict) — future SSE/WS push
        self.reply_path: "str | None" = None  # own socket path (see _ensure)
        self._epath_idx = 0
        # SHARED MAILBOX for in-flight request ids. Several service
        # threads may hold requests open at once (an /status probe racing
        # a /next handler on this single datagram socket): the reader
        # thread that PHYSICALLY receives a reply is not always its
        # owner, so every foreign response is parked here for the
        # matching waiter instead of being dropped. Silent dropping
        # was the observed starvation: the owner burned its 5 s timeout
        # while its reply sat consumed in a stranger's recv loop, and
        # the flapping 'alive' ping made /status report engine-down on
        # a perfectly healthy engine (A6 forensics).
        self._mailbox: "dict" = {}   # rid -> list of parked JSON lines
        self._cv = threading.Condition()

    # ------------------------------------------------------------------ life
    def _ensure(self) -> socket.socket:
        if self._sock is None:
            if not os.path.exists(self.socket_path):
                raise EngineDown(f"socket ausente: {self.socket_path}")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.settimeout(0)  # non-blocking base; recv uses deadline loop
            # Bind OUR socket to a known path so a non-mpv endpoint (the C1
            # mock) can find it: CPython's recvfrom() yields NO sender address
            # for AF_UNIX datagrams (real mpv uses C recvmsg, which does), so
            # the mock cannot reply blindly — the `nvr-reply-to` field below
            # bridges that asymmetry. Real mpv ignores unknown fields.
            self._epath_idx += 1
            self.reply_path = os.path.join(
                tempfile.gettempdir(),
                "nvr-eng-%d-%d.sock" % (os.getpid(), self._epath_idx),
            )
            s.bind(self.reply_path)
            s.connect(self.socket_path)
            self._sock = s
        return self._sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self.reply_path:
            try:
                os.unlink(self.reply_path)
            except OSError:
                pass
            self.reply_path = None

    # ------------------------------------------------------------ primitive
    def cmd(self, *args, timeout: float = 5.0) -> dict:
        """Send one mpv command; return its response dict.

        Raises EngineDown (transport) or RuntimeError (rejected/timeout).
        One conservative retry on transport-class failures (a lost
        datagram or a dropped first contact — rare but observed on the
        C1 mock and possible on real mpv under load): the request is
        resent with a fresh id and the same command. Deliberate engine
        rejections are NEVER retried.
        """
        try:
            return self._cmd_impl(*args, timeout=timeout)
        except RuntimeError as exc:
            if str(exc).startswith("engine recusou"):
                raise
        except EngineDown:
            pass
        return self._cmd_impl(*args, timeout=timeout)

    # -------------------------------------------------- mailboxes (thread
    # safe): the shared park for responses destined to OTHER in-flight
    # request ids, so concurrent waiters never eat each other's replies.
    def _take_mailbox(self, rid: int):
        with self._cv:
            bucket = self._mailbox.pop(rid, None)
        if not bucket:
            return None
        try:
            return json.loads(bucket[-1])  # last delivery wins
        except json.JSONDecodeError:
            return None

    def _park_foreign(self, obj: dict) -> None:
        rid = obj.get("request_id")
        if isinstance(rid, int):
            with self._cv:
                bkt = self._mailbox.setdefault(rid, [])
                bkt.append(json.dumps(obj, separators=(",", ":")))
                if len(bkt) > 8:
                    del bkt[:len(bkt) - 8]
                if len(self._mailbox) > 128:  # cap: dead ids must not leak
                    for k in sorted(self._mailbox)[:-128]:
                        self._mailbox.pop(k, None)
        if "event" in obj and self.event_observer is not None:
            try:
                self.event_observer(obj)
            except Exception:
                pass

    def _accept(self, obj: dict) -> dict:
        self.last_contact = time.time()
        if obj.get("error") not in (None, "success"):
            raise RuntimeError(
                "engine recusou: %r :: %r" % (obj.get("error"), obj))
        return obj

    def _cmd_impl(self, *args, timeout: float = 5.0) -> dict:
        # CONCURRENT-SAFE BY DESIGN (A5 — corridas serializadas, nula
        # perda): the ENTIRE request/response exchange (counter, send,
        # reply wait) happens under a single per-engine lock. Before
        # this, only the counter was guarded; two concurrent callers
        # (e.g. the 5 s state tracker and a /status handler) shared the
        # same datagram socket, one side's recv() could eat the other's
        # reply, the victim burned its 5 s window plus the 10 s retry
        # inside cmd(), and the HTTP handler stalled — a lost-update
        # class fault on the control plane. Serializing exchanges costs
        # nothing at control-plane volume (a few dozen packets/min) and
        # makes the engine safe for any number of concurrent drivers.
        # (PT-BR: a troca inteira — contador, envio e espera da
        # resposta — ocorre sob o lock do engine; dois chamadores
        # concorrentes nunca mais disputam o socket de reply.)
        # Complemento A6: uma resposta que chegue destinada a OUTRO rid
        # em voo é estacionada no mailbox compartilhado para o
        # detentor (nunca descartada) — defesa adicional sobre a
        # serialização, útil ex.: reply tardia após retry de
        # EngineDown. Ver `_take_mailbox`/`_park_foreign`.
        with self._lock:
            self._req_counter += 1
            rid = self._req_counter
            if len(args) == 1 and isinstance(args[0], (list, tuple)):
                command = list(args[0])  # tolerate cmd([...]) as well as cmd(*)
            else:
                command = list(args)
            msg = {"command": command, "request_id": rid}
            if self.reply_path:
                msg["nvr-reply-to"] = self.reply_path
            payload = json.dumps(msg, separators=(",", ":")) + "\n"
            sock = self._ensure()
            # A reply for THIS id may already be parked (an earlier
            # exchange swept it in before we started waiting) — honor
            # it before spending another round trip.
            hit = self._take_mailbox(rid)
            if hit is not None:
                return self._accept(hit)
            try:
                sock.send(payload.encode())
            except (FileNotFoundError, ConnectionRefusedError,
                    BrokenPipeError, OSError) as exc:
                self.close()
                raise EngineDown(str(exc)) from exc
            deadline = time.monotonic() + timeout
            while True:
                hit = self._take_mailbox(rid)
                if hit is not None:
                    return self._accept(hit)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"tempo esgotado aguardando resposta do engine (req {rid})")
                try:
                    sock.settimeout(max(0.05, min(remaining, 0.5)))
                    raw = sock.recv(65536)
                except socket.timeout:
                    continue
                except (OSError,) as exc:
                    self.close()
                    raise EngineDown(str(exc)) from exc
                if not raw:
                    continue
                for piece in raw.decode(errors="replace").split("\n"):
                    piece = piece.strip()
                    if not piece:
                        continue
                    try:
                        obj = json.loads(piece)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("request_id") == rid:
                        return self._accept(obj)
                    self._park_foreign(obj)

    def _cmd_multi(self, commands: "list[list]",
                   timeout: float = 5.0) -> "list[dict]":
        """One lock hold, N commands, N replies (atomic batch).

        A6.2 — CONSISTENT SAMPLES: `get_properties` must not tear a
        read across a state change. Before this, the batch was N
        independent `cmd()` exchanges (the lock released between each),
        so a concurrent transaction — the 5 s state tracker (A5) or a
        pause/resume write — could land INSIDE the batch: the first
        half of the sample described the pre-write world, the second
        half the post-write one. A /status delivering such a hybrid
        drove the bridge's reverse-sync projection AGAINST a just-made
        user gesture (the T3/T4 pause-resume oscillation: the page
        then answered with a phantom /resume of its own). Holding the
        per-engine lock across the whole fan-out orders any
        concurrent exchange either fully before or fully after the
        batch, so a sample is always a coherent view of one engine
        state. Cost: a few extra ms of lock hold at control-plane
        volume — negligible. Foreign replies still park in the shared
        mailbox (A6); events keep flowing to the observer.
        (PT-BR: o lote inteiro — envios e drenagem das respostas —
        acontece sob o lock do engine; uma escrita concorrente nunca
        mais fica entremeada no meio da amostra.)
        """
        with self._lock:
            planned: "list[int]" = []
            payloads: "list[tuple[int, str]]" = []
            for c in commands:
                self._req_counter += 1
                rid = self._req_counter
                planned.append(rid)
                msg = {"command": list(c), "request_id": rid}
                if self.reply_path:
                    msg["nvr-reply-to"] = self.reply_path
                payloads.append(
                    (rid, json.dumps(msg, separators=(",", ":")) + "\n"))
            sock = self._ensure()
            pending: "dict[int, dict | None]" = {}
            for rid, pline in payloads:
                # A reply for this id may already be parked (an earlier
                # exchange swept it in) — honor it without re-sending.
                hit = self._take_mailbox(rid)
                if hit is not None:
                    pending[rid] = hit
                    continue
                try:
                    sock.send(pline.encode())
                except (FileNotFoundError, ConnectionRefusedError,
                        BrokenPipeError, OSError) as exc:
                    self.close()
                    raise EngineDown(str(exc)) from exc
                pending[rid] = None
            deadline = time.monotonic() + timeout
            while any(v is None for v in pending.values()):
                for rid in planned:
                    if pending[rid] is None:
                        hit = self._take_mailbox(rid)
                        if hit is not None:
                            pending[rid] = hit
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "tempo esgotado aguardando respostas do engine "
                        f"(lote de {len(planned)})")
                try:
                    sock.settimeout(max(0.05, min(remaining, 0.5)))
                    raw = sock.recv(65536)
                except socket.timeout:
                    continue
                except (OSError,) as exc:
                    self.close()
                    raise EngineDown(str(exc)) from exc
                if not raw:
                    continue
                for piece in raw.decode(errors="replace").split("\n"):
                    piece = piece.strip()
                    if not piece:
                        continue
                    try:
                        obj = json.loads(piece)
                    except json.JSONDecodeError:
                        continue
                    oid = obj.get("request_id")
                    if (isinstance(oid, int) and oid in pending
                            and pending[oid] is None):
                        pending[oid] = obj
                    else:
                        self._park_foreign(obj)
            reps: "list[dict]" = []
            for rid in planned:
                val = pending[rid]
                if val is None:
                    raise RuntimeError(
                        f"resposta ausente para req {rid} (lote de {len(planned)})")
                reps.append(val)
            return [self._accept(v) for v in reps]

    def batch_cmd(self, commands: "list[list]",
                  timeout: float = 5.0) -> "list[dict]":
        """Several commands as ONE atomic exchange (see `_cmd_multi`).

        Same retry policy as `cmd()`: one conservative retry on
        transport-class failures (lost datagram, timeout); deliberate
        engine rejections ("engine recusou") are NEVER retried.
        """
        try:
            return self._cmd_multi(commands, timeout=timeout)
        except RuntimeError as exc:
            if str(exc).startswith("engine recusou"):
                raise
        except EngineDown:
            pass
        return self._cmd_multi(commands, timeout=timeout)

    # ------------------------------------------------------------ conveniences
    def get_property(self, name: str) -> "object":
        resp = self.cmd(["get_property", name])
        return resp.get("data")

    def get_properties(self, names: "list[str]") -> dict:
        """Fetch several properties as ONE atomic exchange (A6.2).

        Stock mpv's `get_property` takes exactly ONE name; there is no
        multi-name form in the protocol, so the only faithful way to
        read several is N calls — but now sent under a SINGLE lock hold
        (`batch_cmd`/`_cmd_multi`) so a concurrent set_property can
        never land in the middle of the batch: the returned dict is a
        consistent view of one engine state, which is what /status and
        the 5 s tracker rely on for a trustworthy sample.
        """
        replies = self.batch_cmd([["get_property", n] for n in names])
        return {n: r.get("data") for n, r in zip(names, replies)}

    def set_property(self, name: str, value) -> None:
        self.cmd(["set_property", name, value])

    @property
    def alive(self) -> bool:
        try:
            self.cmd(["get_property", "core-idle"])
            return True
        except (EngineDown, RuntimeError, Exception):
            return False
