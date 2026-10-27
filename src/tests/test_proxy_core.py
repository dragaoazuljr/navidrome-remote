"""C1 suite — the M0' acceptance matrix for the transparent proxy.

Maps directly to proposal §6 (NVR-1) clauses:
  (1) B(i):  test_websocket_echo_is_green / test_upgrade_refusal_is_red_documented
  (2) B(ii): test_set_cookie_passthrough_verbatim
  (3) A:     test_options_headers_pass_through  (+ live map in suite/c1_probe.sh)
  (4)        test_html_get_has_bridge_tag_present_and_ordered
  (5)        test_spy_ndjson_records_transactions  (boot-sequence material)
  (6)        covered in test_api_ipc.py (JSON-IPC vs fake sink)
Plus transport mechanics: chunked/gzip transformation, idempotent double
proxy, Location rewrite, static asset, upstream-down 502.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import zlib

from navidrome_remote.config import Config
from navidrome_remote.proxy import ProxyServer
from navidrome_remote.spy import Spy
from .helper_client import request, ws_frame_text, ws_handshake, ws_read_one_frame
from .helper_upstream import FakeUpstream, SESSION_COOKIE_VALUE, DOC_PLAIN

ASSET = b"/* bridge */\nconsole.log('nvra');\n"


def _cfg(port, upstream: FakeUpstream, spy_path, token="") -> Config:
    return Config(
        listen_addr="127.0.0.1",
        listen_port=port,
        upstream_host=upstream.host,
        upstream_port=upstream.port,
        upstream_vhost="",
        mpv_socket="/dev/shm/nvr-test-nope",
        spy_file=spy_path,
        api_token=token,
        keepalive_idle=2.0,  # tests: short idle so aclose()/wait_closed can't stall
    )


class ProxyCoreSuite(unittest.IsolatedAsyncioTestCase):
    """Servers live on a persistent loop in a daemon thread (unittest class
    setup is sync); tests run on their own isolated loop and talk over sockets."""

    @classmethod
    def setUpClass(cls):
        import threading

        cls.tmp = tempfile.TemporaryDirectory(prefix="nvr-test-")
        cls.spy_path = os.path.join(cls.tmp.name, "spy.ndjson")
        cls.free = _pick_free_ports(2)
        cfg_second_layer = Config(
            listen_addr="127.0.0.1",
            listen_port=cls.free[1],
            upstream_host="127.0.0.1",
            upstream_port=cls.free[0],
            keepalive_idle=2.0,
        )
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        fut = asyncio.run_coroutine_threadsafe(cls._bring_up(cfg_second_layer), cls.loop)
        fut.result(timeout=30)

    @classmethod
    async def _bring_up(cls, cfg_second_layer):
        cls.up = await FakeUpstream().start()
        cls.cfg_main = _cfg(cls.free[0], cls.up, cls.spy_path)
        cls.main = ProxyServer(cls.cfg_main, Spy(cls.spy_path), None, ASSET)
        cls.layer2 = ProxyServer(
            cfg_second_layer,
            Spy(os.path.join(cls.tmp.name, "spy2.ndjson")),
            None,
            ASSET,
        )
        await cls.main.start()
        await cls.layer2.start()
        cls.port = cls.free[0]
        cls.port2 = cls.free[1]

    @classmethod
    def tearDownClass(cls):
        try:
            fut = asyncio.run_coroutine_threadsafe(cls._teardown(), cls.loop)
            fut.result(timeout=30)
        finally:
            cls.loop.call_soon_threadsafe(cls.loop.stop)
            cls.loop_thread.join(timeout=10)
            cls.tmp.cleanup()

    @classmethod
    async def _teardown(cls):
        await cls.main.aclose()
        await cls.layer2.aclose()
        await cls.up.aclose()

    def _host(self, port=None):
        return ("127.0.0.1", self.port if port is None else port)

    # ------------------------------------------------------------ acceptance
    async def test_root_302_passthrough_relative_location(self):
        h, p = self._host()
        r = await request(h, p, "GET", "/")
        self.assertEqual(r.status, 302)
        self.assertEqual(r.header("location"), "/app/")

    async def test_html_get_has_bridge_tag_present_and_ordered(self):
        h, p = self._host()
        r = await request(h, p, "GET", "/app/")
        self.assertEqual(r.status, 200)
        self.assertIn("<script src=\"/bridge.js\"></script>", r.text)
        tag_i = r.text.index("<script src=\"/bridge.js\">")
        head_i = r.text.index("</head>")
        self.assertLess(tag_i, head_i, "injection must precede </head>")
        self.assertNotIn("transform_skip", self._spy_events())

    async def test_injection_idempotent_across_two_proxy_layers(self):
        h, p = self._host(self.port2)
        r = await request(h, p, "GET", "/app/")
        self.assertEqual(r.status, 200)
        self.assertEqual(
            r.text.count("<script src=\"/bridge.js\">"),
            1,
            "second proxy layer must not inject twice",
        )

    async def test_chunked_gzip_document_gets_injected(self):
        h, p = self._host()
        r = await request(h, p, "GET", "/app/chunkgz/")
        self.assertEqual(r.status, 200)
        ce = (r.header("content-encoding") or "").lower()
        self.assertTrue(
            ce.startswith("gzip"),
            f"re-emitted body should stay gzip (got {ce!r}); framing was te->cl",
        )
        plain = zlib.decompress(r.body, 16 + zlib.MAX_WBITS).decode()
        self.assertIn("<script src=\"/bridge.js\">", plain)
        self.assertIsNone(r.header("transfer-encoding"))

    async def test_gzip_cl_document_gets_injected(self):
        h, p = self._host()
        r = await request(h, p, "GET", "/app/gz/")
        self.assertEqual(r.status, 200)
        plain = zlib.decompress(r.body, 16 + zlib.MAX_WBITS).decode()
        self.assertIn("<script src=\"/bridge.js\">", plain)

    async def test_static_css_passthrough_without_injection(self):
        h, p = self._host()
        r = await request(h, p, "GET", "/css/main.css")
        self.assertEqual(r.status, 200)
        self.assertNotIn("/bridge.js", r.text)
        self.assertEqual(r.header("access-control-allow-origin"), "*")

    async def test_bridge_asset_served_with_etag(self):
        h, p = self._host()
        r = await request(h, p, "GET", "/bridge.js")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body, ASSET)
        ct = (r.header("content-type") or "").lower()
        self.assertTrue(ct.startswith("application/javascript"))
        etag = r.header("etag")
        self.assertTrue(etag)
        r304 = await request(h, p, "GET", "/bridge.js", {"if-none-match": etag})
        self.assertEqual(r304.status, 304)

    # ------------------------------------------------------------ cookies B(ii)
    async def test_set_cookie_passthrough_verbatim(self):
        h, p = self._host()
        r = await request(h, p, "POST", "/login")
        self.assertEqual(r.status, 200)
        expected = f"nd_session={SESSION_COOKIE_VALUE}; Path=/; HttpOnly"
        self.assertEqual(r.header("set-cookie"), expected, "Set-Cookie must be verbatim")
        # the session survives navigation: replay it on another route
        r2 = await request(h, p, "GET", "/rest/ping", {"cookie": f"nd_session={SESSION_COOKIE_VALUE}"})
        self.assertEqual(r2.status, 200)
        self.assertEqual(r2.text, "pong")
        r3 = await request(h, p, "GET", "/rest/ping")
        self.assertEqual(r3.status, 401)

    # ------------------------------------------------------------ upgrade B(i)
    async def test_websocket_echo_is_green(self):
        h, p = self._host()
        (rd, wr), status, first = await ws_handshake(h, p, "/ws")
        try:
            self.assertEqual(status, 101, "handshake must reach 101 through the proxy")
            payload = "hello"
            wr.write(ws_frame_text(payload))
            await wr.drain()
            got = await ws_read_one_frame(rd)
            self.assertEqual(got, f"echo:{payload}")
        finally:
            try:
                wr.close()
            except (OSError, RuntimeError):
                pass

    async def test_upgrade_refusal_is_red_documented(self):
        """An upstream that REFUSES the upgrade must be surfaced as 426 and
        logged as upgrade_red — that line is the clause-B(i) RED evidence
        (and, per M0' acceptance, a documented RED is a valid outcome)."""
        h, p = self._host()
        # ws_handshake ALREADY consumed the response head; the rest of the
        # exchange (refusal body) rides on the same stream as `first`.
        (rd, wr), status, first = await ws_handshake(h, p, "/ws-red")
        try:
            self.assertEqual(status, 426)
            buf = first
            try:
                while True:
                    chunk = await asyncio.wait_for(rd.read(4096), 2)
                    if not chunk:
                        break
                    buf += chunk
            except (asyncio.TimeoutError, Exception):
                pass
            self.assertIn(b"upgrade-refused", buf)
        finally:
            wr.close()
        events = self._spy_events()
        self.assertTrue(any(e.get("event") == "upgrade_red" for e in events))

    # ------------------------------------------------- cláusula A (mechanics)
    async def test_options_headers_pass_through(self):
        h, p = self._host()
        r = await request(h, p, "OPTIONS", "/opts/")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("access-control-allow-origin"), "*")
        self.assertEqual(r.header("access-control-allow-methods"), "GET")

    async def test_absolute_location_rewritten_to_client_origin(self):
        h, p = self._host()
        r = await request(h, p, "GET", "/loc/")
        self.assertEqual(r.status, 302)
        loc = r.header("location")
        self.assertTrue(loc.startswith(f"http://{h}:{p}/"), loc)
        self.assertTrue(loc.endswith("/app/"), loc)

    # ------------------------------------------------------------ spy (5)
    def _spy_events(self):
        out = []
        with open(self.spy_path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    async def test_spy_ndjson_records_transactions(self):
        h, p = self._host()
        await request(h, p, "GET", "/")
        await request(h, p, "GET", "/app/")
        evs = self._spy_events()
        reqs = [e for e in evs if e.get("event") == "req"]
        ress = [e for e in evs if e.get("event") == "res"]
        self.assertTrue(any(e.get("path") == "/app/" for e in reqs))
        app_res = [e for e in ress if e.get("path") == "/app/" and e.get("status") == 200]
        self.assertTrue(app_res, "res event for /app/ must be present")
        self.assertTrue(all(e.get("transformed") is True for e in app_res))
        # cookie VALUE must never appear (credential invariant)
        blob = json.dumps(evs)
        self.assertNotIn(SESSION_COOKIE_VALUE, blob)

    async def test_upstream_down_returns_502_json(self):
        dead = Config(
            listen_addr="127.0.0.1",
            listen_port=0,
            upstream_host="127.0.0.1",
            upstream_port=1,  # nobody there
            keepalive_idle=2.0,
        )
        port = _pick_free_port()
        dead.listen_port = port
        srv = ProxyServer(dead, Spy(os.path.join(self.tmp.name, "spy3.ndjson")), None, ASSET)
        await srv.start()
        try:
            r = await request("127.0.0.1", port, "GET", "/app/", timeout=8)
            self.assertEqual(r.status, 502)
            obj = json.loads(r.text)
            self.assertEqual(obj.get("error"), "upstream-unreachable")
        finally:
            await srv.aclose()


def _pick_free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _pick_free_ports(n) -> "list[int]":
    return [_pick_free_port() for _ in range(n)]


if __name__ == "__main__":
    unittest.main()
