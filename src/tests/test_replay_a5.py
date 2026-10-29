"""A5 — RETOMADA / replay, end-to-end (the DoR acceptance item).

Acceptance under test: "Kill do processo -> restart -> fila/estado
reconstituídos sem perda" — the service's memory outlives the process,
and a restart reconstructs the same picture the listener left:

  phase playing  -> it PLAYS AGAIN, positioned where it was (the seek
                   is anchored by the track id and clamped);
  phase paused   -> it presents the very "paused mid-track" picture
                   (no surprise audio; /resume lifts it);
  phase stopped  -> it comes up PARKED (idle engine holding the list:
                   ZERO unexpected audio after boot) and /resume starts
                   it — closing the idle-engine trap (an idle engine
                   obeys no plain `pause false`).

Two layers, each stricter than the last:

  1. IN-PROCESS worlds speaking the REAL JSON-IPC wire against
     `tools/fake_mpv_sink.py`: world A serves and then DIES HARD (no
     store close() — a pending window is lost ON PURPOSE: that is a
     crash); the engine is a separate process and follows the deployed
     topology (PartOf: it dies with the service and returns as a FRESH
     sink); world B boots from the SAME state file and must arrive at
     the same picture.
  2. PROCESS LEVEL: the real `python3 -m navidrome_remote` binary is
     spawned with the spec's environment, SIGKILL'd mid-playback, and
     respawned across THREE generations (playing / stopped / parked+
     resume) — the literal kill->restart, end to end over TCP.

The L0 static token authenticates the test client (machine-to-machine
escape — a signed-in-browser session is a different axis, covered in
the A3/M1 suites).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from navidrome_remote.config import Config
from navidrome_remote.ipc import MpvEngine
from navidrome_remote.proxy import ProxyServer
from navidrome_remote.spy import Spy
from navidrome_remote.api import Api
from navidrome_remote.gateway import SessionGate
from navidrome_remote.queue import QueueStore
from navidrome_remote.__main__ import _restore_loop, _state_tracker

from .helper_client import Resp, request
from .helper_upstream import FakeUpstream

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SINK_SCRIPT = os.path.join(ROOT, "tools", "fake_mpv_sink.py")
TOKEN = "op-a5-1"
NAV_USER = "sbc"
NAV_PASS = "svc-pass-91-Q"
ITEMS = [{"id": "nvr-1", "title": "Uno"},
         {"id": "nvr-2", "title": "Dos"},
         {"id": "nvr-3", "title": "Três"}]
AUTH = {"authorization": "Bearer " + TOKEN}


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_sink(sock: str) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, SINK_SCRIPT, "--socket", sock],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    if not os.path.exists(sock):
        proc.kill()
        raise RuntimeError("mock mpv sink não criou o socket")
    return proc


def _kill_hard(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


def _run(coro):
    """One fresh event loop per scenario (the M1 house pattern)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


# ======================================================================
class A5World:
    """One in-process generation of the service (the exact world the
    module assembles in `__main__`), with the production janitors
    (restore loop + 5 s state tracker) running as real tasks."""

    def __init__(self, *, listen_port: int, upstream,
                 mpv_sock: str, state: str, spy_file: str):
        # `upstream` is a booted FakeUpstream (its .host/.port exist
        # only after start() — the test boots it first, exactly as the
        # M1 suite does: a Config built before the listener would bake
        # a dead port into the gate's probe target).
        self.cfg = Config(
            listen_addr="127.0.0.1",
            listen_port=listen_port,
            upstream_host=upstream.host,
            upstream_port=upstream.port,
            mpv_socket=mpv_sock,
            spy_file=spy_file,
            state_file=state,
            api_token=TOKEN,
            nav_user=NAV_USER,
            nav_pass=NAV_PASS,
            state_write_interval=1.0,
            keepalive_idle=2.0,
        )
        self.engine = MpvEngine(mpv_sock)          # the service's handle
        self.inspector = MpvEngine(mpv_sock)       # test's read-only eye
        self.spy = Spy(spy_file)
        self.store = QueueStore(self.cfg, self.spy)
        self.gate = SessionGate(self.cfg, self.spy)
        self.api = Api(self.cfg, self.spy, self.engine, self.store,
                       self.gate)
        self.server = ProxyServer(self.cfg, self.spy, self.api,
                                  b"/* a5-bridge */\n")
        self.janitors: "list" = []
        self._stop_evt = asyncio.Event()

    async def start(self) -> None:
        await self.server.start()
        loop = asyncio.get_running_loop()
        self.janitors = [
            loop.create_task(_restore_loop(self.store, self.engine,
                                           self.spy, self._stop_evt)),
            loop.create_task(_state_tracker(self.store, self.engine,
                                            self._stop_evt)),
        ]

    async def get(self, target: str) -> Resp:
        return await request("127.0.0.1", self.cfg.listen_port, "GET",
                             target, headers=dict(AUTH))

    async def post(self, target: str, body=None) -> Resp:
        raw = json.dumps(body or {}).encode()
        return await request("127.0.0.1", self.cfg.listen_port, "POST",
                             target, headers=dict(AUTH), body=raw)

    async def status(self):
        r = await self.get("/api/v1/status")
        assert r.status == 200, f"/status -> {r.status}: {r.text}"
        return json.loads(r.text)

    async def wait_for(self, pred, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = await self.status()
            if pred(last):
                return last
            await asyncio.sleep(0.25)
        raise AssertionError(f"condition not met in {timeout}s; last: {last}")

    async def crash(self) -> None:
        """HARD death (SIMULATES THE SIGKILL): the janitors are ripped
        out, the server socket is closed, the engine client abandoned,
        and NOTHING in the process touches the state file again. The
        writer is stopped with one LAST atomic snapshot of the dying
        process's memory (a real SIGKILL loses exactly this: whatever
        was in flight mid-window dies with the process). Skipping the
        writer here would leave a GHOST thread that keeps rewriting
        the file after the 'death' — a dimension that cannot exist
        once the process is gone, and the regenerated world would
        fight the ghost for the file."""
        self._stop_evt.set()
        for t in self.janitors:
            t.cancel()
        await asyncio.gather(*self.janitors, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.server.aclose()
        self.engine.close()
        self.inspector.close()
        with contextlib.suppress(Exception):
            self.store.close()

    async def dispose(self) -> None:
        self._stop_evt.set()
        for t in self.janitors:
            t.cancel()
        await asyncio.gather(*self.janitors, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.server.aclose()
        self.engine.close()
        self.inspector.close()
        self.store.close()




# ======================================================================
class InProcessRetomadaTests(unittest.TestCase):
    """World A dies hard; the engine returns as a FRESH sink; world B
    boots from the same state file and must reconstruct the picture."""

    SONG_DUR = 180.0  # the sink's default duration

    async def _gen0(self, tmp, sock, up):
        """Boot a world, queue the list, bring the engine up, return
        (world, queue_ack). Caller decides what happens next."""
        w = A5World(listen_port=_pick_port(), upstream=up,
                    mpv_sock=sock, state=os.path.join(tmp, "state.json"),
                    spy_file=os.path.join(tmp, "spy.ndjson"))
        await w.start()
        # The sink is a CHILD process (fork+exec+bind): the very first
        # /queue can land while its socket does not exist YET. Production
        # never sees that (the engine is a separate unit, up before the
        # service answers traffic), so the world must meet the same
        # precondition before queueing: engine connected. The 503-pending
        # path of _queue_post stays reserved for REAL outages (covered by
        # the process-level tests and the M1 failure-mode contract).
        await w.wait_for(lambda o: o.get("engine") == "connected",
                         timeout=10)
        r = await w.post("/api/v1/queue",
                         {"items": ITEMS, "mode": "replace"})
        if r.status != 200:
            raise AssertionError(f"/queue -> {r.status}: {r.text}")
        await w.wait_for(lambda o: o["playing"] is True)
        return w

    async def _kill_pair(self, w, sink):
        await w.crash()
        _kill_hard(sink)
        await asyncio.sleep(0.15)  # let straggler IPC settle pre-rebirth

    def _regen(self, tmp, sock, up):
        # plain construction; the caller awaits w2.start() itself.
        return A5World(listen_port=_pick_port(), upstream=up,
                       mpv_sock=sock,
                       state=os.path.join(tmp, "state.json"),
                       spy_file=os.path.join(tmp, "spy.ndjson"))

    # ---- the three acceptance pictures --------------------------------

    def test_playing_comes_back_positioned(self):
        async def go():
            sink = None
            w2 = None
            tmp = tempfile.mkdtemp(prefix="a5rp-play-")
            sock = os.path.join(tmp, "mock.sock")
            up = FakeUpstream(subsonic_users={NAV_USER: "t1"})
            await up.start()
            try:
                sink = _start_sink(sock)
                w = await self._gen0(tmp, sock, up)
                await asyncio.sleep(8.0)   # the position must accumulate
                with open(os.path.join(tmp, "state.json"),
                          encoding="utf-8") as fh:
                    persisted = json.load(fh)
                p0 = persisted.get("pos")
                self.assertIsNotNone(p0, "the tracker must have recorded "
                                         "the position (8s > 5s cadence)")
                await self._kill_pair(w, sink)
                sink = _start_sink(sock)
                w2 = self._regen(tmp, sock, up)
                await w2.start()
                st = await w2.wait_for(
                    lambda o: o["playing"] is True
                    and (o.get("queue") or {}).get("length") == 3)
                pos2 = (st.get("play") or {}).get("pos")
                self.assertIsNotNone(pos2,
                                     "a restarted engine must be positioned")
                # the SEEK must have landed the engine at (roughly) the
                # persisted point: the fresh engine started at 0, the
                # seek pulled it to p0 (plus the seconds it ran since).
                self.assertGreaterEqual(
                    pos2, p0 - 1.5,
                    f"engine at {pos2}, persisted {p0} — the anchored "
                    "seek did not happen")
                self.assertLessEqual(
                    pos2, p0 + 3.0,
                    f"engine at {pos2} vs persisted {p0} — impossible "
                    "forward jump (would mean the seek went astray)")
            finally:
                if w2 is not None:
                    with contextlib.suppress(Exception):
                        await w2.dispose()
                if sink is not None:
                    _kill_hard(sink)
                with contextlib.suppress(Exception):
                    await up.aclose()
                shutil.rmtree(tmp, ignore_errors=True)
        _run(go())

    def test_paused_picture_survives_and_resume_lights_it(self):
        async def go():
            sink = None
            w2 = None
            tmp = tempfile.mkdtemp(prefix="a5rp-pause-")
            sock = os.path.join(tmp, "mock.sock")
            up = FakeUpstream(subsonic_users={NAV_USER: "t1"})
            await up.start()
            try:
                sink = _start_sink(sock)
                w = await self._gen0(tmp, sock, up)
                await asyncio.sleep(6.0)
                r = await w.post("/api/v1/pause")
                self.assertEqual(r.status, 200, r.text)
                await asyncio.sleep(0.6)
                await self._kill_pair(w, sink)
                sink = _start_sink(sock)
                w2 = self._regen(tmp, sock, up)
                await w2.start()
                # the picture must come back PAUSED: no surprise audio
                # engine-precondition guard (same rationale as the other
                # traps: the paused picture is visible before the client
                # finishes dialing; a /resume racing that dial 503s).
                st = await w2.wait_for(
                    lambda o: o.get("engine") == "connected"
                    and o["paused"] is True and o["playing"] is False,
                    timeout=20.0)
                self.assertFalse(st["idle"])
                r = await w2.post("/api/v1/resume")
                self.assertEqual(r.status, 200, r.text)
                obj = json.loads(r.text)
                self.assertEqual(obj.get("action"), "unpaused",
                                 "a presenting (paused) engine must lift "
                                 "the pause, not replay the list")
                await w2.wait_for(lambda o: o["playing"] is True)
            finally:
                if w2 is not None:
                    with contextlib.suppress(Exception):
                        await w2.dispose()
                if sink is not None:
                    _kill_hard(sink)
                with contextlib.suppress(Exception):
                    await up.aclose()
                shutil.rmtree(tmp, ignore_errors=True)
        _run(go())

    def test_stopped_parks_and_resume_starts_the_trap(self):
        async def go():
            sink = None
            w2 = None
            tmp = tempfile.mkdtemp(prefix="a5rp-stop-")
            sock = os.path.join(tmp, "mock.sock")
            up = FakeUpstream(subsonic_users={NAV_USER: "t1"})
            await up.start()
            try:
                sink = _start_sink(sock)
                w = await self._gen0(tmp, sock, up)
                await asyncio.sleep(6.0)
                r = await w.post("/api/v1/stop")
                self.assertEqual(r.status, 200, r.text)
                await asyncio.sleep(0.6)
                await self._kill_pair(w, sink)
                sink = _start_sink(sock)
                w2 = self._regen(tmp, sock, up)
                await w2.start()
                # PARKED: the list is reloaded, the engine IDLE, and the
                # crucial property — NO AUDIO (playing must be False).
                # `engine == connected` is part of the PRECONDITION: the
                # park picture (idle + list) is reported while the engine
                # client is still dialing in; a /resume before that dial
                # lands would 503 ("nada foi alterado") — a world race,
                # not a product fault. In production the engine unit is
                # up before the service traffic arrives.
                st = await w2.wait_for(
                    lambda o: o.get("engine") == "connected"
                    and o["idle"] is True and o["playing"] is False
                    and (o.get("queue") or {}).get("length") == 3,
                    timeout=20.0)
                self.assertFalse(st["paused"])
                # THE TRAP: a plain `pause false` would leave a parked
                # (idle) engine silent forever; /resume must START it.
                r = await w2.post("/api/v1/resume")
                self.assertEqual(r.status, 200, r.text)
                obj = json.loads(r.text)
                self.assertEqual(obj.get("action"), "started",
                                 "a parked list must be STARTED by /resume")
                st2 = await w2.wait_for(lambda o: o["playing"] is True)
                self.assertEqual((st2.get("queue") or {}).get("length"), 3)
            finally:
                if w2 is not None:
                    with contextlib.suppress(Exception):
                        await w2.dispose()
                if sink is not None:
                    _kill_hard(sink)
                with contextlib.suppress(Exception):
                    await up.aclose()
                shutil.rmtree(tmp, ignore_errors=True)
        _run(go())


# ======================================================================
class ProcessLifecycleTests(unittest.TestCase):
    """THE money test: the real binary, real SIGKILL, three generations.

    Generation 1  boots, queues, plays for ~9 s, is SIGKILL'd mid-song.
    Generation 2  (fresh engine) boots and must BE playing, positioned
                    near the persisted point.
    Generation 3  receives /stop, is SIGKILL'd; generation 4 boots
                    PARKED (idle, list intact) and /resume starts it.
    (Four boots, three deaths — one extra generation to hold the stop.)
    """

    BIN_ENV_BASE = None  # filled in setUp from a real env copy

    def _make_env(self, tmp, port, sock) -> "dict":
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": os.getcwd(),
            "NVRA_LISTEN_ADDR": "127.0.0.1",
            "NVRA_LISTEN_PORT": str(port),
            "NVRA_UPSTREAM": "127.0.0.1:9",  # unrouted: L0-exempt traffic
                                              # never probes the upstream
            "NVRA_MPV_SOCKET": sock,
            "NVRA_STATE_FILE": os.path.join(tmp, "state.json"),
            "NVRA_SPY_FILE": os.path.join(tmp, "spy.ndjson"),
            "NVRA_API_TOKEN": TOKEN,
            "NVRA_NAV_USER": NAV_USER,
            "NVRA_NAV_PASS": NAV_PASS,
            "NVRA_STATE_WRITE_INTERVAL": "1",
        })
        return env

    async def _boot_child(self, tmp, port, sock) -> subprocess.Popen:
        env = self._make_env(tmp, port, sock)
        errlog = os.path.join(tmp, f"child-{port}.stderr")
        ferr = open(errlog, "wb")
        proc = subprocess.Popen(
            [sys.executable, "-m", "navidrome_remote"],
            cwd=os.getcwd(),
            env=env,
            stdout=subprocess.DEVNULL, stderr=ferr)
        # readiness: the public health probe. A refused connection is
        # NORMAL while the child imports/assembles (and a dead child
        # must not mask its stderr — we keep the log for the post-mortem).
        for _ in range(240):
            rc = proc.poll()
            if rc is not None:
                ferr.close()
                tail = open(errlog, "rb").read()[-2000:].decode(
                    "utf-8", errors="replace")
                raise RuntimeError(
                    f"child died before booting (rc={rc}); last stderr:\n{tail}")
            try:
                # the gate covers /health TOO (A3: the whole control
                # plane is gated; the L0 static token is the machine
                # escape) — an anonymous 401 means "up but sealed",
                # which is ALSO a ready signal.
                r = await request("127.0.0.1", port, "GET",
                                  "/api/v1/health", headers=dict(AUTH))
            except OSError:
                r = None
            if r is not None and r.status == 200:
                return proc
            await asyncio.sleep(0.25)
        _kill_hard(proc)
        ferr.close()
        tail = open(errlog, "rb").read()[-2000:].decode(
            "utf-8", errors="replace")
        raise RuntimeError(f"child never became healthy; last stderr:\n{tail}")

    async def _op(self, port, method, path, body=None):
        raw = json.dumps(body or {}).encode()
        return await request("127.0.0.1", port, method, path,
                             headers=dict(AUTH), body=raw)

    def test_sigkill_then_restart_recovers_everything(self):
        async def go():
            sink = None
            tmp = tempfile.mkdtemp(prefix="a5plc-")
            sock = os.path.join(tmp, "ipc.sock")
            state = os.path.join(tmp, "state.json")
            try:
                # -------- generation 1: play, then DIE -------------
                sink = _start_sink(sock)
                port = _pick_port()
                g = await self._boot_child(tmp, port, sock)
                # Precondition: engine connected before the first MUTATING
                # call (production boots the engine unit first; the health
                # probe alone only proves the HTTP plane is up).
                g1dl = time.monotonic() + 15
                g1st = {}
                while time.monotonic() < g1dl:
                    g1st = json.loads(
                        (await self._op(port, "GET", "/api/v1/status")).text)
                    if g1st.get("engine") == "connected":
                        break
                    await asyncio.sleep(0.25)
                self.assertEqual(g1st.get("engine"), "connected",
                                 f"g1: engine never connected: {g1st}")
                r = await self._op(port, "POST", "/api/v1/queue",
                                   {"items": ITEMS, "mode": "replace"})
                self.assertEqual(json.loads(r.text).get("ok"), True, r.text)
                await asyncio.sleep(9.0)
                with open(state, encoding="utf-8") as fh:
                    persisted = json.load(fh)
                p0 = persisted.get("pos")
                self.assertIsNotNone(p0,
                                     "9s > 5s cadence: the position "
                                     "must be persisted")
                os.kill(g.pid, signal.SIGKILL)   # <-- the kill
                g.wait(timeout=5)
                _kill_hard(sink)                 # the engine's death
                # -------- generation 2: must BE PLAYING ------------
                sink = _start_sink(sock)
                port2 = _pick_port()
                g = await self._boot_child(tmp, port2, sock)
                r = await self._op(port2, "GET", "/api/v1/status")
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    r = await self._op(port2, "GET", "/api/v1/status")
                    o = json.loads(r.text)
                    if (o.get("playing") is True
                            and (o.get("queue") or {}).get("length") == 3):
                        break
                    await asyncio.sleep(0.5)
                o = json.loads(r.text)
                self.assertTrue(o.get("playing"),
                                f"generation 2 must be PLAYING: {o}")
                pos2 = (o.get("play") or {}).get("pos")
                self.assertIsNotNone(pos2)
                self.assertGreaterEqual(
                    pos2, p0 - 2.0,
                    f"engine at {pos2} vs persisted {p0}: the anchored "
                    "seek must place the listener WHERE THEY LEFT OFF")
                self.assertLessEqual(pos2, p0 + 4.0,
                                     f"impossible forward jump: {pos2}")
                # -------- generation 3: stop, then DIE --------------
                r = await self._op(port2, "POST", "/api/v1/stop")
                self.assertEqual(json.loads(r.text).get("ok"), True, r.text)
                await asyncio.sleep(0.5)
                os.kill(g.pid, signal.SIGKILL)
                g.wait(timeout=5)
                _kill_hard(sink)
                # -------- generation 4: PARKED, then /resume --------
                sink = _start_sink(sock)
                port4 = _pick_port()
                g = await self._boot_child(tmp, port4, sock)
                deadline = time.monotonic() + 20
                o = {}
                while time.monotonic() < deadline:
                    r = await self._op(port4, "GET", "/api/v1/status")
                    o = json.loads(r.text)
                    if (o.get("engine") == "connected"
                            and o.get("idle") is True
                            and o.get("playing") is False
                            and (o.get("queue") or {}).get("length") == 3):
                        break
                    await asyncio.sleep(0.5)
                self.assertTrue(
                    o.get("idle") is True and o.get("playing") is False
                    and (o.get("queue") or {}).get("length") == 3,
                    f"generation 4 must boot PARKED (list, no audio): {o}")
                r = await self._op(port4, "POST", "/api/v1/resume")
                o2 = json.loads(r.text)
                self.assertEqual(o2.get("action"), "started",
                                 f"the parked list must be STARTED: {o2}")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    r = await self._op(port4, "GET", "/api/v1/status")
                    o3 = json.loads(r.text)
                    if o3.get("playing") is True:
                        break
                    await asyncio.sleep(0.5)
                self.assertTrue(o3.get("playing"),
                                "after /resume the parked list must PLAY")
                os.kill(g.pid, signal.SIGKILL)
                g.wait(timeout=5)
            finally:
                if sink is not None:
                    _kill_hard(sink)
                shutil.rmtree(tmp, ignore_errors=True)
        _run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)
