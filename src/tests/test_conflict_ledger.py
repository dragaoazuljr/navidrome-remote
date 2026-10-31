"""A8 — LAST-WRITE-WINS conflict log (the plane ledger on /api/v1/*).

Card acceptance (2): "Conflito simultaneo (UI vs comando externo) =
last-write-wins COM log.".

The world is hermetic (no sockets): the production Api assembled as usual
(cfg/spy/engine/store/controller) with a FAKE GATEWAY (L1..L3 is out of
scope here — every call is authorized) and a FAKE MPV ENGINE (duck-typed:
the controller emits its stock commands against it), plus an INJECTABLE
monotonic clock so the CONFLICT_WINDOW_S (3 s) window is drivable without
sleeping.

Planes:
  ui  — the bridge marks its writes with the header X-NVR-Plane: ui;
  ext — external callers without the marker (CLI/tools);
  op  — the static-token (L0) operator plane.

Asserted: the plane_conflict spy event fields (winner, loser, resource,
routes, gap-ms, seq); the cumulative /status counter ("conflicts"); and
the NEGATIVE cases that must NOT log: same-plane races, gaps beyond the
window, and different resource classes.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest

from navidrome_remote.config import Config
from navidrome_remote import api as W
from navidrome_remote.api import Api
from navidrome_remote.gateway import Verdict
from navidrome_remote.queue import QueueStore
from navidrome_remote.spy import Spy


# ---------------------------------------------------------------- fakes

class FakeEngine:
    """Duck-typed stand-in for ipc.MpvEngine: the controller only ever
    calls cmd()/get_property()/get_properties()/alive against it."""

    def __init__(self) -> None:
        self.commands = []
        self.alive_flag = True
        self.props = {
            "idle": False,
            "pause": False,
            "volume": 0.5,
            "shuffle": 0,
            "loop-file": "none",
            "loop-playlist": "none",
            "playlist-count": 2,
            "playlist-current": -1,
            "duration": 100.0,
            "media-title": "Faixa Teste",
            "playback-status": "playing",
        }

    @property
    def alive(self) -> bool:
        return self.alive_flag

    def cmd(self, *args, timeout: float = 5.0) -> dict:
        if args and isinstance(args[0], (list, tuple)):
            cmd = list(args[0])
        else:
            cmd = list(args)
        self.commands.append(cmd)
        if cmd[:1] == ["set_property"] and len(cmd) >= 3:
            self.props[cmd[1]] = cmd[2]
        if cmd[:1] == ["loadfile"]:
            self.props["playlist-current"] = 1
        return {}

    def get_property(self, name: str):
        return self.props.get(name)

    def get_properties(self, names):
        return {n: self.props.get(n) for n in names}


class FakeGateway:
    """L1..L3 collapsed to an unconditional allowance: the ledger is the
    subject; the authorization layers have their own suites (M1)."""

    def __init__(self, spy) -> None:
        self.spy = spy

    async def evaluate(self, headers, target, payload) -> Verdict:
        return Verdict(True, 200, "session-ok", "", "jwt")


class Hdrs:
    """Minimal get_first shim mirroring the transport's header object."""

    def __init__(self, data: dict) -> None:
        self.data = {k.lower(): v for k, v in data.items()}

    def get_first(self, name: str, default=None):
        return self.data.get(str(name).lower(), default)


class Clock:
    """Injectable monotonic clock; the tests advance it explicitly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ------------------------------------------------------------------ world

def _mk_world(api_token: str = ""):
    d = tempfile.mkdtemp(prefix="a8-ledger-")
    cfg = Config(
        listen_addr="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=80,
        mpv_socket=os.path.join(d, "mpv-ipc"),
        spy_file=os.path.join(d, "spy.ndjson"),
        api_token=api_token,
        state_file=os.path.join(d, "state.json"),
        state_write_interval=0.0,
    )
    with open(cfg.state_file, "w", encoding="utf-8") as fh:
        fh.write("{}")
    spy = Spy(cfg.spy_file)
    engine = FakeEngine()
    store = QueueStore(cfg, spy)
    clock = Clock()
    api = Api(cfg, spy, engine, store=store,
              gateway=FakeGateway(spy), clock=clock)
    return cfg, spy, engine, store, clock, api


# A page-shaped JWT (three segments): the L0 escape hatch REQUIRES a
# candidate when a static token is configured (a call presenting nothing
# is 401'd before L1..L3). Production UI calls always present the page's
# own session — the test world mirrors that.
PAGE_JWT = "eyJhbGciOiJub25lIn0.fakepage.session"


async def call(api, method: str, path: str, payload=None, *,
               plane: str | None = None, bearer=None):
    """`bearer` defaults to the page session (the real UI shape). Pass
    `bearer=""` to present NOTHING (rejected by L0 by design) or the
    static token to ride the operator plane."""
    if bearer is None:
        bearer = PAGE_JWT
    headers = {}
    if plane is not None:
        headers["x-nvr-plane"] = plane
    if bearer:
        headers["authorization"] = "bearer " + bearer
    body = json.dumps(payload or {}).encode()
    return await api.handle(method, path, Hdrs(headers), body)


# ------------------------------------------------------------------ tests

class ConflictLedgerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = _mk_world(api_token="tok-a8")
        cls.cfg, cls.spy, cls.engine, cls.store, cls.clock, cls.api = \
            cls.world
        cls.events = []
        # funnel the spy events into a recorder (the file sink keeps
        # working independently).
        orig = cls.spy.event

        async def record(kind, **fields):
            cls.events.append({"kind": kind, **fields})
            return await orig(kind, **fields)

        cls.spy.event = record

    async def _status_body(self):
        _, _, body = await call(self.api, "GET", "/api/v1/status")
        return json.loads(body)

    def _conflicts_so_far(self):
        return [e for e in self.events
                if e["kind"] == "plane_conflict"]

    # -------------------------------------------------- positive cases

    async def test_a_ui_write_beats_a_preceding_external_one(self):
        """The card's headline race: UI vs external on the same resource,
        inside the window -> one plane_conflict (winner = the later)."""
        self.events.clear()
        self.clock.advance(0)
        s, _, b = await call(self.api, "POST", "/api/v1/volume",
                             {"level": 50})
        self.assertEqual(s, 200)  # external (no marker)
        self.clock.advance(0.5)
        s, _, b = await call(self.api, "POST", "/api/v1/volume",
                             {"level": 60}, plane="ui")
        self.assertEqual(s, 200)
        cf = self._conflicts_so_far()
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["winner"], "ui")
        self.assertEqual(cf[0]["loser"], "ext")
        self.assertEqual(cf[0]["resource"], "volume")
        self.assertEqual(cf[0]["route"], "/api/v1/volume")
        self.assertEqual(cf[0]["prev_route"], "/api/v1/volume")
        self.assertEqual(cf[0]["gap_ms"], 500)
        self.assertEqual(cf[0]["seq"], 1)
        st = await self._status_body()
        self.assertEqual(st["conflicts"], 1)

    async def test_b_external_last_beats_ui_first(self):
        """Symmetric: UI first, external last -> the EXTERNAL one wins."""
        self.events.clear()
        self.clock.advance(10)
        s, _, _ = await call(self.api, "POST", "/api/v1/seek",
                             {"pos": 20}, plane="ui")
        self.assertEqual(s, 200)
        self.clock.advance(1.0)
        s, _, _ = await call(self.api, "POST", "/api/v1/seek", {"pos": 80})
        self.assertEqual(s, 200)
        cf = self._conflicts_so_far()
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["winner"], "ext")
        self.assertEqual(cf[0]["loser"], "ui")
        self.assertEqual(cf[0]["resource"], "seek")

    async def test_c_operator_plane_vs_ui(self):
        """Static-token (L0) writes are the 'op' plane; a racing UI write
        on the same resource logs the conflict."""
        self.events.clear()
        self.clock.advance(20)
        s, _, _ = await call(self.api, "POST", "/api/v1/shuffle",
                             {"value": 1}, bearer="tok-a8")
        self.assertEqual(s, 200)
        self.clock.advance(0.25)
        s, _, _ = await call(self.api, "POST", "/api/v1/shuffle",
                             {"value": 0}, plane="ui")
        self.assertEqual(s, 200)
        cf = self._conflicts_so_far()
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["winner"], "ui")
        self.assertEqual(cf[0]["loser"], "op")

    # ------------------------------------------------- negative cases

    async def test_d_same_plane_race_does_not_log(self):
        self.events.clear()
        self.clock.advance(30)
        before = (await self._status_body())["conflicts"]
        for _ in range(3):
            s, _, _ = await call(self.api, "POST", "/api/v1/repeat",
                                 {"mode": "track"}, plane="ui")
            self.assertEqual(s, 200)
            self.clock.advance(0.5)
        self.assertEqual(self._conflicts_so_far(), [])
        self.assertEqual((await self._status_body())["conflicts"], before)

    async def test_e_gap_beyond_window_does_not_log(self):
        self.events.clear()
        self.clock.advance(40)
        s, _, _ = await call(self.api, "POST", "/api/v1/volume",
                             {"level": 10}, plane="ui")
        self.assertEqual(s, 200)
        self.clock.advance(3.01)  # just past the 3 s window
        s, _, _ = await call(self.api, "POST", "/api/v1/volume",
                             {"level": 20})
        self.assertEqual(s, 200)
        self.assertEqual(self._conflicts_so_far(), [])

    async def test_f_different_resource_classes_do_not_log(self):
        self.events.clear()
        self.clock.advance(50)
        s, _, _ = await call(self.api, "POST", "/api/v1/volume",
                             {"level": 30}, plane="ui")
        self.assertEqual(s, 200)
        self.clock.advance(0.1)
        s, _, _ = await call(self.api, "POST", "/api/v1/seek", {"pos": 5})
        self.assertEqual(s, 200)
        self.assertEqual(self._conflicts_so_far(), [])

    async def test_g_read_requests_never_touch_the_ledger(self):
        """
        Reads (GET /status, /queue) never touch the ledger: the
        snapshot before/after must be identical, and no read-only
        route may ever land in it.
        """
        self.events.clear()
        self.clock.advance(60)
        before = dict(self.api._ledger)
        for method, path in (("GET", "/api/v1/queue"),
                             ("GET", "/api/v1/status")):
            s, _, _ = await call(self.api, method, path, plane="ui")
            self.assertEqual(s, 200)
        after = dict(self.api._ledger)
        self.assertEqual(before, after)
        for k in after:
            self.assertIn(k, W._CONFLICT_CLASSES.values())  # only write-class keys, ever
    async def test_h_rejected_write_is_not_a_winner(self):
        """A 4xx rejection is NOT an accepted write: racing an accepted
        one, it leaves the ledger entry of the accepted write intact."""
        self.events.clear()
        self.clock.advance(70)
        s, _, _ = await call(self.api, "POST", "/api/v1/seek",
                             {"pos": 10}, plane="ui")
        self.assertEqual(s, 200)
        self.clock.advance(0.2)
        s, _, _ = await call(self.api, "POST", "/api/v1/seek",
                             {"pos": -5})
        self.assertEqual(s, 400)  # rejected: negative position
        self.assertEqual(self._conflicts_so_far(), [])
        # the NEXT accepted external write (still inside the window of
        # the ORIGINAL ui write's class entry) wins against the UI one.
        self.clock.advance(0.5)
        s, _, _ = await call(self.api, "POST", "/api/v1/seek", {"pos": 6})
        self.assertEqual(s, 200)
        cf = self._conflicts_so_far()
        self.assertEqual(len(cf), 1)
        self.assertEqual((cf[0]["winner"], cf[0]["loser"]), ("ext", "ui"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
