"""C1 suite — M0' acceptance (6): /api/v1/* JSON-IPC against the FAKE sink.

Harness: PLAIN unittest.TestCase + one brand-new event loop per test
(``self._run``). Deliberately NOT IsolatedAsyncioTestCase: its Runner
(debug=True, ContextVar snapshot) interacts nondeterministically with a
hand-rolled HTTP pipeline (observed: intermittent total hangs, zero output).
The per-test loop here mirrors ``asyncio.run``, proven deterministic.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from navidrome_remote.api import Api, subsonic_stream_url
from navidrome_remote.config import Config
from navidrome_remote.ipc import MpvEngine
from navidrome_remote.proxy import ProxyServer
from navidrome_remote.spy import Spy
from .helper_client import request
from .helper_upstream import FakeUpstream

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SINK_SCRIPT = os.path.join(ROOT, "tools", "fake_mpv_sink.py")
ASSET = b"/* bridge */\n"


def _pick_free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _jpost(host, port, target, obj):
    """POST a JSON object as the BODY (helper_client's 5th positional arg is
    headers — a footgun this helper exists to prevent)."""
    return await request(host, port, "POST", target, body=json.dumps(obj).encode())


class ApiIpcSuite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nvr-api-")
        self.socket = os.path.join(self.tmp.name, "mock-ipc.sock")
        self.proc = subprocess.Popen(
            [sys.executable, SINK_SCRIPT, "--socket", self.socket],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if os.path.exists(self.socket):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(self.socket), "mock sink failed to start")

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ util
    def _run(self, coro_factory):
        """Fresh loop per call (mirrors asyncio.run, proven deterministic)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro_factory())
        finally:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            asyncio.set_event_loop(None)

    async def _ctx(self):
        """Bring up (fake upstream + proxy-with-API) on the CURRENT loop."""
        up = await FakeUpstream().start()
        port = _pick_free_port()
        cfg = Config(
            listen_addr="127.0.0.1",
            listen_port=port,
            upstream_host=up.host,
            upstream_port=up.port,
            mpv_socket=self.socket,
            spy_file=os.path.join(self.tmp.name, "spy.ndjson"),
            keepalive_idle=2.0,
        )
        engine = MpvEngine(self.socket)
        api = Api(cfg, Spy(cfg.spy_file), engine)
        srv = ProxyServer(cfg, Spy(cfg.spy_file), api, ASSET)
        await srv.start()
        return up, port, srv, engine

    # ------------------------------------------------------------------ tests
    def test_health_reports_engine_connected(self):
        def go():
            async def inner():
                up, port, srv, engine = await self._ctx()
                try:
                    r = await request("127.0.0.1", port, "GET", "/api/v1/health")
                    self.assertEqual(r.status, 200)
                    obj = json.loads(r.text)
                    self.assertTrue(obj["ok"])
                    self.assertEqual(obj["engine"], "connected")
                    self.assertEqual(obj["navidrome_anchor"], "v0.64.2")
                finally:
                    await srv.aclose()
                    await up.aclose()
                    engine.close()

            return inner()

        self._run(go)

    def test_status_idle_then_playing_then_paused(self):
        def go():
            async def inner():
                up, port, srv, engine = await self._ctx()
                try:
                    r = await request("127.0.0.1", port, "GET", "/api/v1/status")
                    self.assertTrue(json.loads(r.text)["idle"])

                    r = await _jpost(
                        "127.0.0.1",
                        port,
                        "/api/v1/play",
                        {"url": "http://192.168.0.200:4533/rest/stream?id=42&u=x&s=y"},
                    )
                    self.assertEqual(r.status, 200)
                    self.assertTrue(json.loads(r.text)["accepted"])

                    await asyncio.sleep(0.3)
                    r = await request("127.0.0.1", port, "GET", "/api/v1/status")
                    obj = json.loads(r.text)
                    self.assertFalse(obj["idle"])
                    self.assertTrue(obj["playing"])
                    self.assertIsNotNone(obj["media_title"])

                    r = await request("127.0.0.1", port, "POST", "/api/v1/pause")
                    self.assertEqual(r.status, 200)
                    r = await request("127.0.0.1", port, "GET", "/api/v1/status")
                    obj = json.loads(r.text)
                    self.assertTrue(obj["paused"])
                    self.assertFalse(obj["playing"])

                    r = await request("127.0.0.1", port, "POST", "/api/v1/resume")
                    self.assertEqual(r.status, 200)
                finally:
                    await srv.aclose()
                    await up.aclose()
                    engine.close()

            return inner()

        self._run(go)

    def test_seek_volume_next_previous_stop(self):
        def go():
            async def inner():
                up, port, srv, engine = await self._ctx()
                try:
                    await _jpost("127.0.0.1", port, "/api/v1/play", {"url": "http://x/y.opus?a=1"})
                    r = await _jpost("127.0.0.1", port, "/api/v1/seek", {"pos": 42})
                    self.assertEqual(r.status, 200)
                    r = await request("127.0.0.1", port, "GET", "/api/v1/status")
                    self.assertAlmostEqual(json.loads(r.text)["time_pos"], 42.0, delta=0.5)

                    r = await _jpost("127.0.0.1", port, "/api/v1/volume", {"level": 30})
                    self.assertEqual(r.status, 200)

                    r = await request("127.0.0.1", port, "POST", "/api/v1/next")
                    self.assertEqual(r.status, 200)

                    r = await request("127.0.0.1", port, "POST", "/api/v1/stop")
                    self.assertEqual(r.status, 200)
                    r = await request("127.0.0.1", port, "GET", "/api/v1/status")
                    self.assertTrue(json.loads(r.text)["idle"])
                finally:
                    await srv.aclose()
                    await up.aclose()
                    engine.close()

            return inner()

        self._run(go)

    def test_validation_errors(self):
        def go():
            async def inner():
                up, port, srv, engine = await self._ctx()
                try:
                    r = await request("127.0.0.1", port, "POST", "/api/v1/play", body=b"{}")
                    self.assertEqual(r.status, 400)
                    r = await request("127.0.0.1", port, "POST", "/api/v1/seek", body=b'{"pos":-5}')
                    self.assertEqual(r.status, 400)
                    r = await request("127.0.0.1", port, "POST", "/api/v1/volume", body=b'{"level":101}')
                    self.assertEqual(r.status, 400)
                    r = await request("127.0.0.1", port, "GET", "/api/v1/nope")
                    self.assertEqual(r.status, 404)
                    r = await request("127.0.0.1", port, "POST", "/api/v1/play", body=b"not-json")
                    self.assertEqual(r.status, 400)
                finally:
                    await srv.aclose()
                    await up.aclose()
                    engine.close()

            return inner()

        self._run(go)

    def test_play_by_song_id_requires_credentials_501(self):
        def go():
            async def inner():
                up, port, srv, engine = await self._ctx()
                try:
                    r = await _jpost("127.0.0.1", port, "/api/v1/play", {"id": "42"})
                    self.assertEqual(r.status, 501)
                    self.assertIn("sem-credenciais", json.loads(r.text)["error"])
                finally:
                    await srv.aclose()
                    await up.aclose()
                    engine.close()

            return inner()

        self._run(go)

    def test_engine_down_reports_503(self):
        def go():
            async def inner():
                bad_sock = os.path.join(self.tmp.name, "no-such-socket")
                up = await FakeUpstream().start()
                bad = Config(
                    listen_addr="127.0.0.1",
                    listen_port=_pick_free_port(),
                    upstream_host=up.host,
                    upstream_port=up.port,
                    mpv_socket=bad_sock,
                    spy_file=os.path.join(self.tmp.name, "spy-503.ndjson"),
                    keepalive_idle=2.0,
                )
                bad_api = Api(bad, Spy(bad.spy_file), MpvEngine(bad_sock))
                bad_srv = ProxyServer(bad, Spy(bad.spy_file), bad_api, ASSET)
                await bad_srv.start()
                try:
                    r = await request("127.0.0.1", bad.listen_port, "GET", "/api/v1/status")
                    self.assertEqual(r.status, 200)
                    self.assertEqual(json.loads(r.text)["engine"], "down")
                    r2 = await _jpost("127.0.0.1", bad.listen_port, "/api/v1/play", {"url": "u"})
                    self.assertEqual(r2.status, 503)
                    self.assertEqual(json.loads(r2.text)["error"], "engine-down")
                finally:
                    await bad_srv.aclose()
                    await up.aclose()

            return inner()

        self._run(go)

    def test_token_gate(self):
        def go():
            async def inner():
                up = await FakeUpstream().start()
                gated = Config(
                    listen_addr="127.0.0.1",
                    listen_port=_pick_free_port(),
                    upstream_host=up.host,
                    upstream_port=up.port,
                    mpv_socket=self.socket,
                    spy_file=os.path.join(self.tmp.name, "spy-token.ndjson"),
                    api_token="sekret-token",
                    keepalive_idle=2.0,
                )
                gated_api = Api(gated, Spy(gated.spy_file), MpvEngine(self.socket))
                gated_srv = ProxyServer(gated, Spy(gated.spy_file), gated_api, ASSET)
                await gated_srv.start()
                try:
                    r = await request("127.0.0.1", gated.listen_port, "GET", "/api/v1/health")
                    self.assertEqual(r.status, 401)
                    r = await request(
                        "127.0.0.1",
                        gated.listen_port,
                        "GET",
                        "/api/v1/health",
                        {"authorization": "Bearer sekret-token"},
                    )
                    self.assertEqual(r.status, 200)
                finally:
                    await gated_srv.aclose()
                    await up.aclose()

            return inner()

        self._run(go)

    def test_subsonic_stream_url_shape(self):
        url = subsonic_stream_url(
            "http://192.168.0.200:4533", "42", "svcuser", "Sup3rSecret!"
        )
        self.assertTrue(url.startswith("http://192.168.0.200:4533/rest/stream?"))
        import base64 as _b64

        salt = _b64.b64encode(b"Sup3rSecret!").decode()[:12]
        self.assertIn(f"s={salt}", url)
        self.assertIn("u=svcuser", url)
        self.assertIn("id=42", url)
        self.assertNotIn("Sup3rSecret!", url)


if __name__ == "__main__":
    unittest.main()
