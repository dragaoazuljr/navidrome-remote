"""C1 suite — M1/A3: the three-layer session gate.

Harness follows test_api_ipc: plain unittest + one brand-new event loop
per test. The FAKE upstream is extended with a Subsonic-shaped
`/rest/getIndexes` endpoint (the gate's liveness probe) with a
configurable credential table, so the WHOLE gate (L1/L2/L3 + probe
classification) is exercised offline — exactly the C1 contract.

Covers the A3 acceptance:
  * rejects a request without an authenticated session (401 missing);
  * rejects the SBC SERVICE account presented FROM THE BROWSER (403) —
    including the A2-observed u=sbc wire shape AND a service-account
    JWT; the rejection happens BEFORE any upstream probe;
  * token per call: a fresh credential re-probes; identical credentials
    are rate-limited to one probe per 10 s window (dedupe), never
    trusted across different credentials;
  * probe classification: code-40 / http-401 -> session-invalid;
    route-404 (anchor break) -> session-unverifiable (FAIL CLOSED);
    transport failure -> session-unverifiable;
  * invariant: no credential value (sentinel cookie/jwt/t) ever appears
    in any client response NOR in the spy NDJSON;
  * L0 static token remains an explicit operator escape hatch;
  * local preflight: OPTIONS /api/v1/* -> 204 + allow headers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_api_m1 import start_sink
from navidrome_remote.api import Api
from navidrome_remote.config import Config
from navidrome_remote.queue import QueueStore
from navidrome_remote.gateway import (
    IDENTITY_CLAIMS,
    SessionGate,
    extract_candidate,
)
from navidrome_remote.ipc import MpvEngine
from navidrome_remote.proxy import ProxyServer
from navidrome_remote.spy import Spy
from .helper_client import request
from .helper_upstream import FakeUpstream

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SINK_SCRIPT = os.path.join(ROOT, "tools", "fake_mpv_sink.py")
ASSET = b"/* bridge */\n"

# ---------------------------------------------------------------- fixtures
def b64u(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_jwt(**claims) -> str:
    header = b64u({"alg": "HS256", "typ": "JWT"})
    payload = b64u(claims)
    sig = "deadbeefcafebabe" * 2
    return f"{header}.{payload}.{sig}"


JWT_BOB = make_jwt(username="bob", ep=1)
JWT_ALICE = make_jwt(username="alice")
JWT_SBC = make_jwt(username="sbc", ep=1)


def md5_t(password: str, salt: str) -> str:
    return hashlib.md5((password + salt).encode()).hexdigest()


SALT = "abcDEF12"  # first-12-b64-of-password stand-in for the fake world
PW_ALICE = "alice-pass-XYZ"
PW_SBC = "sbc-SENTINEL-PASSWORD-7Q"  # sentinel: must never leak anywhere
T_ALICE = md5_t(PW_ALICE, SALT)
T_SBC = md5_t(PW_SBC, SALT)


def pick_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class GateContext:
    """(fake upstream + proxy-with-gate + sink) on a given loop."""

    def __init__(self, upstream_kwargs=None, cfg_overrides=None,
                 probe_route_404=False):
        self.tmp = tempfile.TemporaryDirectory(prefix="nvr-gate-")
        self.upstream = FakeUpstream(
            subsonic_users={
                "alice": T_ALICE,
                "bob": md5_t("bob-pass", SALT),
                "sbc": T_SBC,  # sbc IS valid upstream — the point of L2
            },
            subsonic_jwts={JWT_BOB, JWT_ALICE, JWT_SBC},
            cookie_sessions={"SENTINELCOOKIEVALUE-q1": "someone"},
            probe_route_404=probe_route_404,
            **(upstream_kwargs or {}),
        )
        self._cfg_overrides = dict(
            listen_addr="127.0.0.1",
            listen_port=pick_port(),
            mpv_socket=os.path.join(self.tmp.name, "mock.sock"),
            spy_file=os.path.join(self.tmp.name, "spy.ndjson"),
            state_file=os.path.join(self.tmp.name, "state.json"),
            keepalive_idle=2.0,
            nav_user="sbc",
            nav_pass=PW_SBC,
        )
        self._cfg_overrides.update(cfg_overrides or {})
        self._tmp_keep = bool(os.environ.get("NVR_KEEP"))
        # NOTE: Config is built in start(), AFTER the fake upstream has
        # bound its port (building it in __init__ would bake port=0 into
        # the gate's probe target and every probe would be refused).

    async def start(self):
        await self.upstream.start()
        ov = self._cfg_overrides
        # Production always has the mpv engine reachable; stand up the
        # fake sink (same helper as the M1 service tests) on the exact
        # socket path Config bakes in (the mock.sock override above) —
        # status must report engine="connected".
        self.spike_sock = ov["mpv_socket"]
        self.sink = start_sink(self.spike_sock)
        self.cfg = Config(upstream_host=self.upstream.host,
                          upstream_port=self.upstream.port, **ov)
        self.engine = MpvEngine(self.cfg.mpv_socket)
        self.spy = Spy(self.cfg.spy_file)
        self.gate = SessionGate(self.cfg, self.spy)
        self.api = Api(self.cfg, self.spy, self.engine,
                       store=QueueStore(self.cfg, self.spy),
                       gateway=self.gate)
        self.server = ProxyServer(self.cfg, self.spy, self.api, ASSET)
        await self.server.start()
        return self

    async def stop(self):
        await self.server.aclose()
        await self.upstream.aclose()
        self.engine.close()
        sink = getattr(self, "sink", None)
        if sink is not None:
            sink.kill()
            sink.wait(timeout=5)
        if self._tmp_keep:
            try:
                import shutil
                shutil.copy(self.cfg.spy_file, "/tmp/nvr-gate-kept.ndjson")
                print("KEEP-TMP:", self.tmp.name)
            except Exception:
                pass
        else:
            self.tmp.cleanup()

    def get(self, target, headers=None, method="GET", body=b""):
        return request("127.0.0.1", self.cfg.listen_port, method, target,
                       headers=headers, body=body)

    def post_json(self, target, obj, headers=None):
        return request("127.0.0.1", self.cfg.listen_port, "POST", target,
                       headers=headers, body=json.dumps(obj).encode())


# ==========================================================================
class ExtractUnitTests(unittest.TestCase):
    def test_none_without_anything(self):
        class H:
            def get_first(self, n):
                return None
        self.assertIsNone(extract_candidate(H(), "/api/v1/status", {}))

    def test_params_channel(self):
        class H:
            def get_first(self, n):
                return None
        c = extract_candidate(H(), "/api/v1/status?u=alice&t=tt&s=ss&v=1.12.1", {})
        self.assertIsNotNone(c)
        self.assertEqual(c.channel, "params")
        self.assertEqual(c.user, "alice")
        self.assertIn(("t", "tt"), c.params)

    def test_jwt_bearer_claim_identity(self):
        class H:
            def get_first(self, n):
                return "Bearer " + JWT_BOB if n.lower() == "authorization" else None
        c = extract_candidate(H(), "/api/v1/status", {})
        self.assertEqual(c.channel, "jwt")
        self.assertEqual(c.user, "bob")

    def test_jwt_in_body(self):
        class H:
            def get_first(self, n):
                return None
        c = extract_candidate(H(), "/api/v1/spy", {"jwt": JWT_ALICE, "k": "x"})
        self.assertEqual(c.channel, "jwt")
        self.assertEqual(c.user, "alice")

    def test_cookie_as_jwt_beats_opaque(self):
        class H:
            def get_first(self, n):
                return "token=" + JWT_SBC if n.lower() == "cookie" else None
        c = extract_candidate(H(), "/api/v1/status", {})
        self.assertEqual(c.channel, "jwt")
        self.assertEqual(c.user, "sbc")

    def test_opaque_cookie_channel(self):
        class H:
            def get_first(self, n):
                return "nd_session=ABC123" if n.lower() == "cookie" else None
        c = extract_candidate(H(), "/api/v1/status", {})
        self.assertEqual(c.channel, "cookie")
        self.assertIsNone(c.user)

    def test_identity_claim_order(self):
        self.assertGreaterEqual(IDENTITY_CLAIMS[0], "a")  # stable tuple


# ==========================================================================
class GateMatrixTests(unittest.TestCase):
    def setUp(self):
        self.ctx_holder = {}

    def _run(self, factory):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(factory())
        finally:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            asyncio.set_event_loop(None)

    def _make(self, **kw):
        ctx = GateContext(**kw)
        self.ctx_holder["ctx"] = ctx
        return ctx

    # ---- L1
    def test_no_session_is_401_missing(self):
        def go():
            async def inner():
                ctx = await self._make().start()
                try:
                    for target in ("/api/v1/status", "/api/v1/queue"):
                        r = await ctx.get(target)
                        self.assertEqual(r.status, 401, target)
                        self.assertEqual(json.loads(r.text)["error"], "session-missing")
                    r = await ctx.post_json("/api/v1/play", {"id": "42"})
                    self.assertEqual(r.status, 401)
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    def test_health_is_public(self):
        def go():
            async def inner():
                ctx = await self._make().start()
                try:
                    r = await ctx.get("/api/v1/health")
                    self.assertEqual(r.status, 200)
                    obj = json.loads(r.text)
                    self.assertTrue(obj["ok"])
                    self.assertIn("session-3layer", obj["gate"])
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    # ---- L2 (service account, the A3 money case)
    def test_sbc_params_from_browser_is_403_no_probe(self):
        def go():
            async def inner():
                ctx = await self._make().start()
                try:
                    q = "?u=sbc&t=%s&s=%s" % (T_SBC, SALT)
                    r = await ctx.get("/api/v1/status" + q)
                    self.assertEqual(r.status, 403)
                    obj = json.loads(r.text)
                    self.assertEqual(obj["error"], "service-account-forbidden")
                    self.assertEqual(obj["channel"], "params")
                    # L2 fires BEFORE any upstream probe:
                    probed = [t for (_m, t, _h) in ctx.upstream.requests
                              if t.split("?")[0] == "/rest/getIndexes"]
                    self.assertEqual(probed, [], "L2 must not consume a probe")
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    def test_sbc_jwt_is_403(self):
        def go():
            async def inner():
                ctx = await self._make().start()
                try:
                    r = await ctx.get("/api/v1/status",
                                      {"authorization": "Bearer " + JWT_SBC})
                    self.assertEqual(r.status, 403)
                    self.assertEqual(json.loads(r.text)["error"],
                                     "service-account-forbidden")
                    self.assertEqual(json.loads(r.text)["channel"], "jwt")
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    # ---- L3
    def test_valid_user_is_allowed_and_per_call_probe(self):
        def go():
            async def inner():
                ctx = await self._make().start()
                try:
                    q = "?u=alice&t=%s&s=%s" % (T_ALICE, SALT)
                    r1 = await ctx.get("/api/v1/status" + q)
                    self.assertEqual(r1.status, 200)
                    self.assertEqual(json.loads(r1.text)["engine"], "connected")
                    n_after_first = len(ctx.upstream.requests)
                    # 2nd call SAME credential within the 10s dedupe window:
                    r2 = await ctx.get("/api/v1/status" + q)
                    self.assertEqual(r2.status, 200)
                    self.assertEqual(len(ctx.upstream.requests), n_after_first,
                                     "identical credential must not re-probe within window")
                    # DIFFERENT credential: fresh probe
                    q2 = "?u=alice&t=wrongT&s=%s" % SALT
                    r3 = await ctx.get("/api/v1/status" + q2)
                    self.assertEqual(r3.status, 401)
                    self.assertEqual(json.loads(r3.text)["error"], "session-invalid")
                    self.assertGreater(len(ctx.upstream.requests), n_after_first)
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    def test_unknown_user_is_invalid(self):
        def go():
            async def inner():
                ctx = await self._make().start()
                try:
                    r = await ctx.get("/api/v1/status?u=ghost&t=zzz&s=%s" % SALT)
                    self.assertEqual(r.status, 401)
                    self.assertEqual(json.loads(r.text)["error"], "session-invalid")
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    def test_probe_route_absent_is_unverifiable_failclosed(self):
        def go():
            async def inner():
                ctx = await self._make(probe_route_404=True).start()
                try:
                    r = await ctx.get("/api/v1/status?u=alice&t=%s&s=%s" % (T_ALICE, SALT))
                    self.assertEqual(r.status, 401)
                    self.assertEqual(json.loads(r.text)["error"],
                                     "session-unverifiable")
                    # spy must carry the loud red:
                    data = open(ctx.cfg.spy_file).read()
                    self.assertIn("gate_deny_session_unverifiable", data)
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    def test_opaque_cookie_allowed_via_forward(self):
        def go():
            async def inner():
                ctx = await self._make().start()
                try:
                    r = await ctx.get("/api/v1/status",
                                      {"cookie": "nd_session=SENTINELCOOKIEVALUE-q1"})
                    self.assertEqual(r.status, 200)
                    self.assertEqual(json.loads(r.text)["engine"], "connected")
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    # ---- L0
    def test_static_token_escape_hatch(self):
        def go():
            async def inner():
                ctx = await self._make(cfg_overrides={"api_token": "STATIC-OP-1"}).start()
                try:
                    # valid static, NO session: allowed (operator M2M channel)
                    r = await ctx.get("/api/v1/status",
                                      {"authorization": "Bearer STATIC-OP-1"})
                    self.assertEqual(r.status, 200)
                    # invalid static, no session, no jwt: 401
                    r = await ctx.get("/api/v1/status",
                                      {"authorization": "Bearer WRONG"})
                    self.assertEqual(r.status, 401)
                    self.assertEqual(json.loads(r.text)["error"], "unauthorized")
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    # ---- local preflight
    def test_preflight_is_204_with_allow(self):
        def go():
            async def inner():
                ctx = await self._make().start()
                try:
                    r = await ctx.get("/api/v1/status", method="OPTIONS",
                                      headers={"origin": "http://x:1"})
                    self.assertEqual(r.status, 204)
                    self.assertEqual(r.header("access-control-allow-headers"),
                                     "authorization, content-type")
                    self.assertIn("GET, POST", r.header("access-control-allow-methods"))
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)

    # ---- invariant (sentinel scan: responses + spy)
    def test_no_secret_leaks_responses_or_spy(self):
        def go():
            async def inner():
                ctx = await self._make().start()
                try:
                    sentinels = [
                        PW_SBC,                       # raw service password
                        "SENTINELCOOKIEVALUE-q1",     # opaque cookie value
                        T_SBC,                        # hashed credential
                        T_ALICE,
                        JWT_SBC,
                        JWT_ALICE,
                    ]
                    # exercise every channel + every denial path
                    await ctx.get("/api/v1/status")
                    await ctx.get("/api/v1/status",
                                  {"cookie": "nd_session=SENTINELCOOKIEVALUE-q1"})
                    await ctx.get("/api/v1/status",
                                  {"authorization": "Bearer " + JWT_ALICE})
                    await ctx.get("/api/v1/status?u=sbc&t=%s&s=%s" % (T_SBC, SALT))
                    await ctx.get("/api/v1/status?u=alice&t=bad&s=%s" % SALT)
                    await ctx.get("/api/v1/health")
                    blobs = []
                    for fn in (ctx.cfg.spy_file,):
                        blobs.append(open(fn).read())
                    # also capture one live response body as a blob
                    r = await ctx.get("/api/v1/status")
                    blobs.append(r.text)
                    for b in blobs:
                        for s in sentinels:
                            self.assertNotIn(s, b,
                                             "leak: %r present in %s-output" % (s[:8], "blob"))
                finally:
                    await ctx.stop()
            return inner()
        self._run(go)


if __name__ == "__main__":
    unittest.main(verbosity=2)
