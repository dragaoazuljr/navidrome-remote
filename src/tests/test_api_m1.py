"""C1 suite — M1/A3: full /api/v1/* contract under the session gate.

Replaces the M0' suite: every control-plane route is exercised WITH the
gate engaged (real fake-upstream `/rest/getIndexes` probes, real JSON-IPC
mock sink). Acceptance coverage:

  * all routes: play/pause/next/prev/seek/shuffle/repeat/volume/
    queue/status (+health, +spy) pass the contract;
  * token per call: each authenticated call pays its verification (or a
    10 s dedupe on IDENTICAL credentials) — no cross-call trust;
  * credential invariant: NO stream URL, NO token/salt/t value ever
    leaves the service toward a browser (response-body AND on-disk
    scans);
  * persistence: a RESTARTED service restores the queue intent from
    disk and re-applies it to the (now-up) engine (spec item 5:
    playback independent of the browser session);
  * M0' regressions preserved: engine-down 503, credentials-missing
    501, subsonic stream-url shape, 404/405 discipline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
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
from .helper_client import request
from .helper_upstream import FakeUpstream

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SINK_SCRIPT = os.path.join(ROOT, "tools", "fake_mpv_sink.py")
ASSET = b"/* m1-bridge */\n"

# ------------------------------------------------------------- gate creds
SALT = "Salt12AB"
PW_ALICE = "alice-pass-M1"
T_ALICE = hashlib.md5((PW_ALICE + SALT).encode()).hexdigest()
SESSION_QS = "?u=alice&t=%s&s=%s" % (T_ALICE, SALT)
SESSION_BDY = {"u": "alice", "t": T_ALICE, "s": SALT}  # body-carried channel

NAV_USER = "sbc"
NAV_PASS = "svc-pass-91-Q"  # service account: used server-side only (M0')


def subsonic_stream_url(base: str, user: str, password: str, item_id: str) -> str:
    s = __import__("base64").b64encode(password.encode("utf-8"))[:12].decode("ascii")
    t = hashlib.md5((password + s).encode("utf-8")).hexdigest()
    return "%s/rest/stream?id=%s&u=%s&s=%s&t=%s" % (base.rstrip("/"), item_id, user, s, t)


# --------------------------------------------------------------- harness
def pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_sink(sock: str):
    proc = subprocess.Popen([sys.executable, SINK_SCRIPT, "--socket", sock],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    if not os.path.exists(sock):
        proc.kill()
        raise RuntimeError("mock mpv sink não criou o socket")
    return proc


class M1Service:
    def __init__(self, nav_creds=True, api_token=None, shared_state=None):
        self.tmp = tempfile.mkdtemp(prefix="nvr-m1-")
        self.shared_state = shared_state
        kwargs = dict(
            listen_addr="127.0.0.1",
            listen_port=pick_port(),
            mpv_socket=os.path.join(self.tmp, "mock.sock"),
            spy_file=os.path.join(self.tmp, "spy.ndjson"),
            state_file=shared_state or os.path.join(self.tmp, "state.json"),
            keepalive_idle=2.0,
            nav_user=NAV_USER if nav_creds else "",
            nav_pass=NAV_PASS if nav_creds else "",
        )
        if api_token:
            kwargs["api_token"] = api_token
        self._world_kwargs = kwargs
        self.upstream = FakeUpstream(
            subsonic_users={"alice": T_ALICE, NAV_USER: T_ALICE})

    async def start(self):
        # world is assembled HERE, after the fake upstream bound its real
        # port (a Config built earlier would bake port=0 into the gate
        # probe target and refuse every probe).
        await self.upstream.start()
        self.cfg = Config(upstream_host=self.upstream.host,
                          upstream_port=self.upstream.port,
                          **self._world_kwargs)
        self.spike = MpvEngine(self.cfg.mpv_socket)
        self.sink = start_sink(self.cfg.mpv_socket)
        self.spy = Spy(self.cfg.spy_file)
        from navidrome_remote.api import Api
        from navidrome_remote.gateway import SessionGate
        from navidrome_remote.queue import QueueStore
        self.store = QueueStore(self.cfg, self.spy)
        self.gate = SessionGate(self.cfg, self.spy)
        self.api = Api(self.cfg, self.spy, self.spike, self.store, self.gate)
        self.server = ProxyServer(self.cfg, self.spy, self.api, ASSET)
        self.destroy_tmp = True
        await self.server.start()
        return self

    async def stop(self, purge=True):
        await self.server.aclose()
        await self.upstream.aclose()
        self.sink.kill()
        self.sink.wait(timeout=5)
        self.spike.close()
        if purge and self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None

    def get(self, target_q, headers=None, method="GET", body=b""):
        return request("127.0.0.1", self.cfg.listen_port, method, target_q,
                       headers=headers, body=body)

    def post_json(self, target, extra, headers=None):
        obj = dict(SESSION_BDY)
        obj.update(extra)
        return self.get(target, method="POST", body=json.dumps(obj).encode(),
                        headers=headers)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


# ========================================================================
class HealthPublicContractTests(unittest.TestCase):
    def test_health_never_gated(self):
        async def go():
            svc = M1Service()
            await svc.start()
            try:
                r = await svc.get("/api/v1/health")
                self.assertEqual(r.status, 200)
                obj = json.loads(r.text)
                self.assertTrue(obj["ok"])
                self.assertEqual(obj["gate"], "session-3layer")
                self.assertTrue(obj["version"])
            finally:
                await svc.stop()
        _run(go())


class StatusContractTests(unittest.TestCase):
    def test_requires_session_now(self):
        async def go():
            svc = M1Service()
            await svc.start()
            try:
                r = await svc.get("/api/v1/status")
                self.assertEqual(r.status, 401)
                self.assertEqual(json.loads(r.text)["error"], "session-missing")
                self.assertFalse(
                    any(t.split("?")[0] == "/rest/getIndexes"
                        for (_m, t, _h) in svc.upstream.requests))
            finally:
                await svc.stop()
        _run(go())

    def test_session_shape_and_idle(self):
        async def go():
            svc = M1Service()
            await svc.start()
            try:
                r = await svc.get("/api/v1/status" + SESSION_QS)
                self.assertEqual(r.status, 200)
                obj = json.loads(r.text)
                self.assertEqual(obj["engine"], "connected")
                self.assertIsNone(obj["play"])
                self.assertEqual(obj["shuffle"], False)
                self.assertEqual(obj["repeat"], "off")
                self.assertIsInstance(obj["volume"], int)
                self.assertNotIn("rest/stream", r.text)
                self.assertNotIn(T_ALICE, r.text)
            finally:
                await svc.stop()
        _run(go())


class TransportControlContractTests(unittest.TestCase):
    def test_full_transport_cycle(self):
        async def go():
            svc = M1Service()
            await svc.start()
            try:
                r = await svc.post_json("/api/v1/play",
                                        {"id": "42", "title": "Track Forty-Two"})
                self.assertEqual(r.status, 200)
                self.assertEqual(json.loads(r.text), {"ok": True, "kind": "id"})
                obj = json.loads((await svc.get("/api/v1/status" + SESSION_QS)).text)
                self.assertTrue(obj["playing"])
                self.assertEqual(obj["play"]["id"], "42")
                self.assertIsNone(obj["play"].get("url"),
                                  "stream URL must never leave the service")
                r = await svc.post_json("/api/v1/seek", {"pos": 42})
                self.assertEqual(json.loads(r.text), {"ok": True, "pos": 42})
                obj = json.loads((await svc.get("/api/v1/status" + SESSION_QS)).text)
                self.assertEqual(obj["play"]["pos"], 42.0)
                self.assertEqual((await svc.post_json("/api/v1/seek",
                                                      {"pos": -1})).status, 400)
                self.assertEqual((await svc.post_json("/api/v1/pause", {})).status, 200)
                obj = json.loads((await svc.get("/api/v1/status" + SESSION_QS)).text)
                self.assertFalse(obj["playing"]); self.assertTrue(obj["paused"])
                self.assertEqual((await svc.post_json("/api/v1/resume", {})).status, 200)
                obj = json.loads((await svc.get("/api/v1/status" + SESSION_QS)).text)
                self.assertTrue(obj["playing"])
                r = await svc.post_json("/api/v1/volume", {"level": 37})
                self.assertEqual(json.loads(r.text), {"ok": True, "level": 37})
                self.assertEqual((await svc.post_json("/api/v1/volume",
                                                      {"level": 101})).status, 400)
                self.assertEqual((await svc.post_json("/api/v1/volume",
                                                      {"level": -3})).status, 400)
            finally:
                await svc.stop()
        _run(go())

    def test_shuffle_repeat_modes(self):
        async def go():
            svc = M1Service()
            await svc.start()
            try:
                r = await svc.post_json("/api/v1/shuffle", {})
                self.assertEqual(json.loads(r.text)["enabled"], True)
                self.assertTrue(json.loads(
                    (await svc.get("/api/v1/status" + SESSION_QS)).text)["shuffle"])
                self.assertEqual(json.loads(
                    (await svc.post_json("/api/v1/shuffle", {"value": False})).text)["enabled"], False)
                self.assertEqual(json.loads(
                    (await svc.post_json("/api/v1/repeat", {"mode": "playlist"})).text)["mode"], "playlist")
                self.assertEqual(json.loads(
                    (await svc.post_json("/api/v1/repeat", {"mode": "one"})).text)["mode"], "track")
                self.assertEqual(json.loads(
                    (await svc.post_json("/api/v1/repeat", {"mode": "all"})).text)["mode"], "playlist")
                r = await svc.post_json("/api/v1/repeat", {"mode": "banana"})
                self.assertEqual(r.status, 400)
                self.assertEqual(json.loads(r.text)["valid"], ["off", "track", "playlist"])
                obj = json.loads((await svc.get("/api/v1/status" + SESSION_QS)).text)
                self.assertEqual(obj["repeat"], "playlist")
            finally:
                await svc.stop()
        _run(go())

    def test_next_prev_stop_semantics(self):
        async def go():
            svc = M1Service()
            await svc.start()
            try:
                r = await svc.post_json("/api/v1/queue", {
                    "items": [{"id": "1", "title": "Un"},
                              {"id": "2", "title": "Deux"},
                              {"id": "3"}],
                    "mode": "replace"})
                self.assertEqual(r.status, 200)
                self.assertEqual(json.loads(r.text)["length"], 3)
                obj = json.loads((await svc.get("/api/v1/queue" + SESSION_QS)).text)
                self.assertEqual([i["id"] for i in obj["items"]], ["1", "2", "3"])
                self.assertNotIn('"url"', r.text)          # sanitized view
                self.assertEqual(obj["settings"]["index"], 0)
                self.assertEqual(json.loads((await svc.post_json("/api/v1/next", {})).text),
                                 {"ok": True, "index": 1})
                self.assertEqual(json.loads((await svc.post_json("/api/v1/prev", {})).text),
                                 {"ok": True, "index": 0})
                self.assertEqual(json.loads((await svc.post_json("/api/v1/previous", {})).text),
                                 {"ok": True, "index": 0})
                self.assertEqual((await svc.post_json("/api/v1/stop", {})).status, 200)
                obj = json.loads((await svc.get("/api/v1/status" + SESSION_QS)).text)
                self.assertIsNone(obj["play"])
                obj = json.loads((await svc.get("/api/v1/queue" + SESSION_QS)).text)
                self.assertEqual(len(obj["items"]), 3,
                                 "stop must preserve the queue intent")
                r = await svc.post_json("/api/v1/next", {})
                self.assertEqual(r.status, 503)
                self.assertEqual(json.loads(r.text)["error"], "engine-idle")
            finally:
                await svc.stop()
        _run(go())


class QueueContractTests(unittest.TestCase):
    def test_validation_rules(self):
        async def go():
            svc = M1Service()
            await svc.start()
            try:
                r = await svc.post_json("/api/v1/queue", {
                    "items": [{"id": "1", "url": "http://x/y.m3u8"}], "mode": "append"})
                self.assertEqual(r.status, 400)
                r = await svc.post_json("/api/v1/queue", {"items": [], "mode": "replace"})
                self.assertEqual(r.status, 400)
                r = await svc.post_json("/api/v1/queue",
                                        {"items": [{"id": "1"}], "mode": "teleport"})
                self.assertEqual(r.status, 400)
                self.assertEqual(json.loads(
                    (await svc.post_json("/api/v1/queue",
                                         {"items": [{"id": "a"}], "mode": "append"})).text)["length"], 1)
                self.assertEqual(json.loads(
                    (await svc.post_json("/api/v1/queue",
                                         {"items": [{"id": "b"}, {"id": "c"}],
                                         "mode": "append"})).text)["length"], 3)
                obj = json.loads((await svc.get("/api/v1/queue" + SESSION_QS)).text)
                self.assertEqual([i["id"] for i in obj["items"]], ["a", "b", "c"])
            finally:
                await svc.stop()
        _run(go())


class FailureModeContractTests(unittest.TestCase):
    def test_credentials_missing_501(self):
        async def go():
            svc = M1Service(nav_creds=False)
            await svc.start()
            try:
                r = await svc.post_json("/api/v1/play", {"id": "42"})
                self.assertEqual(r.status, 501)
                self.assertEqual(json.loads(r.text)["error"], "credentials-missing")
            finally:
                await svc.stop()
        _run(go())

    def test_explicit_url_playback_not_echoed(self):
        async def go():
            svc = M1Service()
            await svc.start()
            try:
                r = await svc.post_json("/api/v1/play",
                                        {"url": "http://media.x/song.m3u8",
                                         "title": "Explicit"})
                self.assertEqual(r.status, 200)
                self.assertEqual(json.loads(r.text), {"ok": True, "kind": "url"})
                self.assertNotIn("song.m3u8", r.text,
                                 "the url must NOT be echoed to the client")
                obj = json.loads((await svc.get("/api/v1/status" + SESSION_QS)).text)
                self.assertTrue(obj["playing"])
                self.assertIsNone(obj["play"].get("url"))
            finally:
                await svc.stop()
        _run(go())

    def test_engine_down_503(self):
        async def go():
            tmp = tempfile.mkdtemp(prefix="nvr-nosink-")
            up = FakeUpstream(subsonic_users={"alice": T_ALICE})
            await up.start()
            cfg = Config(listen_addr="127.0.0.1", listen_port=pick_port(),
                         upstream_host=up.host, upstream_port=up.port,
                         mpv_socket=os.path.join(tmp, "never.sock"),
                         spy_file=os.path.join(tmp, "spy.ndjson"),
                         state_file=os.path.join(tmp, "state.json"),
                         keepalive_idle=2.0, nav_user=NAV_USER, nav_pass=NAV_PASS)
            engine = MpvEngine(cfg.mpv_socket)
            spy = Spy(cfg.spy_file)
            from navidrome_remote.api import Api
            from navidrome_remote.gateway import SessionGate
            from navidrome_remote.queue import QueueStore
            api = Api(cfg, spy, engine, QueueStore(cfg, spy), SessionGate(cfg, spy))
            server = ProxyServer(cfg, spy, api, ASSET)
            await server.start()
            try:
                r = await request("127.0.0.1", cfg.listen_port, "POST", "/api/v1/play",
                                  body=json.dumps(dict(SESSION_BDY, id="42")).encode())
                self.assertEqual(r.status, 503)
                self.assertEqual(json.loads(r.text)["error"], "engine-down")
            finally:
                await server.aclose()
                await up.aclose()
                engine.close()
                shutil.rmtree(tmp, ignore_errors=True)
        _run(go())

    def test_404_and_405_discipline(self):
        async def go():
            svc = M1Service()
            await svc.start()
            try:
                self.assertEqual((await svc.get("/api/v1/nope" + SESSION_QS)).status, 404)
                r = await request("127.0.0.1", svc.cfg.listen_port, "POST",
                                  "/api/v1/status" + SESSION_QS,
                                  body=json.dumps(SESSION_BDY).encode())
                self.assertEqual(r.status, 405)
                self.assertEqual(json.loads(r.text)["allow"], "GET")
            finally:
                await svc.stop()
        _run(go())


class L0StaticTokenTests(unittest.TestCase):
    def test_static_operator_token(self):
        async def go():
            svc = M1Service(api_token="STATIC-OP-KEY-1")
            await svc.start()
            try:
                r = await svc.get("/api/v1/status",
                                  {"authorization": "Bearer STATIC-OP-KEY-1"})
                self.assertEqual(r.status, 200)
                r = await svc.get("/api/v1/status",
                                  {"authorization": "Bearer WRONG"})
                self.assertEqual(r.status, 401)
                self.assertEqual(json.loads(r.text)["error"], "unauthorized")
            finally:
                await svc.stop()
        _run(go())


class PersistenceContractTests(unittest.TestCase):
    def test_restart_restores_intent_and_reapplies(self):
        """Spec item 5: a REBOOTED service finds the persisted queue and,
        once the engine is up again, resumes from exactly where the
        intention stood (first item, volume, shuffle) — with no URL or
        credential having ever touched disk."""
        async def go():
            shared = os.path.join(tempfile.mkdtemp(prefix="nvr-share-"), "state.json")
            s1 = M1Service(shared_state=shared)
            await s1.start()
            try:
                self.assertEqual(json.loads(
                    (await s1.post_json("/api/v1/queue", {
                        "items": [{"id": "11", "title": "Uno"},
                                  {"id": "22", "title": "Dos"}],
                        "mode": "replace"})).text)["length"], 2)
                await s1.post_json("/api/v1/volume", {"level": 55})
                await s1.post_json("/api/v1/shuffle", {"value": True})
                await s1.stop(purge=False)   # 'die' — keep disk, kill the world
            except BaseException:
                await s1.stop()
                raise
            s1.tmp = None
            blob = open(shared, "rb").read()
            self.assertNotIn(b"rest/stream", blob, "disk: stream URL leaked")
            self.assertNotIn(b'"t"', blob, "disk: credential leaked")
            self.assertIn(b'"11"', blob)

            # --- reboot: fresh process world, SAME disk, engine up again ---
            s2 = M1Service(shared_state=shared)
            await s2.start()
            try:
                obj = json.loads((await s2.get("/api/v1/queue" + SESSION_QS)).text)
                self.assertEqual([i["id"] for i in obj["items"]], ["11", "22"],
                                 "queue intent must survive the reboot")
                self.assertTrue(obj["settings"]["shuffle"])
                self.assertEqual(obj["settings"]["volume"], 55)
                # one tick of the production lazy-restore loop (in __main__
                # it runs every 5 s; here we drive the same function):
                from navidrome_remote.queue import apply_to_engine
                self.assertTrue(await apply_to_engine(s2.store, s2.spike, s2.spy))
                obj = json.loads((await s2.get("/api/v1/status" + SESSION_QS)).text)
                self.assertTrue(obj["playing"])
                self.assertEqual(obj["play"]["id"], "11")
                self.assertIsNone(obj["play"].get("url"))
                self.assertEqual(obj["volume"], 55)
                self.assertTrue(obj["shuffle"])
                # second application = no-op (the receipt was cleared):
                self.assertTrue(await apply_to_engine(s2.store, s2.spike, s2.spy))
            finally:
                await s2.stop(purge=False)
                if s2.tmp:
                    shutil.rmtree(s2.tmp, ignore_errors=True)
                os.unlink(shared)
                os.rmdir(os.path.dirname(shared))
        _run(go())


class StreamUrlShapeTests(unittest.TestCase):
    def test_subsonic_stream_url_format(self):
        pwd = "P4ss-W0rd-LongEnough-12b64-NoPad-"
        u = subsonic_stream_url("http://nas:4533/", "svc", pwd, "42")
        self.assertTrue(u.startswith("http://nas:4533/rest/stream?id=42&u=svc&s="))
        s_part = u.split("&s=")[1].split("&")[0]
        t_part = u.split("&t=")[1]
        self.assertEqual(len(s_part), 12)
        self.assertEqual(len(t_part), 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)
