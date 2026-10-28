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
        """
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
        try:
            sock.send(payload.encode())
        except (FileNotFoundError, ConnectionRefusedError, BrokenPipeError, OSError) as exc:
            self.close()
            raise EngineDown(str(exc)) from exc
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"tempo esgotado aguardando resposta do engine (req {rid})")
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
            for msg in raw.decode(errors="replace").split("\n"):
                msg = msg.strip()
                if not msg:
                    continue
                try:
                    obj = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if obj.get("request_id") == rid:
                    self.last_contact = time.time()
                    if obj.get("error") not in (None, "success"):
                        raise RuntimeError(f"engine recusou: {obj.get('error')} :: {obj}")
                    return obj
                if "event" in obj and self.event_observer is not None:
                    try:
                        self.event_observer(obj)
                    except Exception:
                        pass

    # ------------------------------------------------------------ conveniences
    def get_property(self, name: str) -> "object":
        resp = self.cmd(["get_property", name])
        return resp.get("data")

    def get_properties(self, names: "list[str]") -> dict:
        """Fetch several properties with one get_property call EACH.

        Stock mpv's `get_property` takes exactly ONE name; there is no
        multi-name form in the protocol, so the only faithful way to read
        several is N calls (control-plane volume: a few dozen/min — cheap
        on an AF_UNIX datagram socket).
        """
        out = {}
        for n in names:
            resp = self.cmd(["get_property", n])
            out[n] = resp.get("data")
        return out

    def set_property(self, name: str, value) -> None:
        self.cmd(["set_property", name, value])

    @property
    def alive(self) -> bool:
        try:
            self.cmd(["get_property", "core-idle"])
            return True
        except (EngineDown, RuntimeError, Exception):
            return False
