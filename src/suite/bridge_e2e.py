#!/usr/bin/env python3
# pyright: reportOptionalMemberAccess = false
# pyright: reportUnusedCoroutine = false
# (test harness: rig.tab/rig.drv are guaranteed present at runtime once
# bring-up succeeds; the None branch is only reachable pre-bring-up)
"""bridge_e2e.py (A6) — browser-level ACCEPTANCE matrix for bridge.js.

Hermetic (C1), real-Chrome: brings up, on loopback,

  * the fake Navidrome upstream  (tests/helper_upstream.FakeUpstream,
    extended in A6 with /rest/stream silence + /fixtures/<page>);
  * the fake mpv sink            (tools/fake_mpv_sink.py — the REGISTERED
    C1 stand-in for the playback engine);
  * the service itself           (python -m navidrome_remote) talking to
    both, with a RANDOM operator token and the SERVICE account
    credentials the production EnvironmentFile carries;
  * a real headless Chrome driven through suite/cdp.py (Tab: navigate,
    evaluate, network/console collectors).

The bridge under test is the SHIPPED asset (assets/bridge.js) served by
the service and injected into the fixture documents by the proxy's HTML
transform — i.e. the E2E exercises the exact production pipeline:

    browser -> proxy (inject) -> bridge.js -> /api/v1/* -> gate L0..L3
          -> controller -> mpv JSON-IPC -> engine (mock)
          <- /status poll <- ...

Scenarios (T1..T15) — see the module tail for the full acceptance map:
passive/pre-login inertia, play/pause/resume/seek/volume/next/prev/
stop/shuffle/repeat translation into /api/v1/* with the PAGE'S session,
engine->UI reverse sync (incl. engine-pushed current-track re-trigger),
gate 403 (service account) with native-mode restoration, anchor-drift
refusal, re-auth recovery, and the credential-invariant scan over every
artifact (spy.ndjson, CDP dump, report) plus the no-upstream-consumption
assertion while engaged.

Execution (dev-ws):
  python3 suite/bridge_e2e.py [--scenario T1,T2,..] [--chrome BIN]
                             [--report-dir DIR] [--keep-going]
Exit code 0 iff all selected scenarios pass.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

SUITE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SUITE_DIR, "..", ".."))
SRC_DIR = os.path.normpath(os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, SUITE_DIR)
sys.path.insert(0, SRC_DIR)

import cdp  # noqa: E402
from tests.helper_upstream import FakeUpstream  # noqa: E402

ASSET_PATH = os.path.join(SRC_DIR, "navidrome_remote", "assets", "bridge.js")
FAKE_MPV = os.path.join(SRC_DIR, "tools", "fake_mpv_sink.py")

# ---------------------------------------------------------------------
# Synthetic identities (values are FIXED here so the invariant scan can
# hunt them; they mirror what the fixture pages DERIVE in-page).
# ---------------------------------------------------------------------
HDR_JSON = json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":"))


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).rstrip(b"=").decode("ascii")


def _jwt(user: str) -> str:
    payload = json.dumps(
        {"username": user, "sub": user,
         "iat": 1793200000, "exp": 1793805600},
        separators=(",", ":"),
    )
    return _b64url(HDR_JSON) + "." + _b64url(payload) + ".fakesigfakesig"


ALICE_JWT = _jwt("fxt_alice")
SBC_JWT = _jwt("sbc_service")
SBC_SERVICE_USER = "sbc_service"
SBC_SERVICE_PASS = "fxt_servpass_0123"  # server-side only (env)
FAKE_U = "fxt_user"                     # the fixture's stream user
FAKE_T = "a" * 40                      # the fixture's stream "t" (hex-like)
FAKE_S = "b" * 12                      # the fixture's stream "s"
TRACK_IDS = ["S1", "S2", "S3"]

# The sentinel set hunted by the invariant scan (T15).
SENTINELS = [ALICE_JWT, SBC_JWT, FAKE_T, FAKE_S, SBC_SERVICE_PASS]
SENTINEL_JWT_RX = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{2,}")


# ---------------------------------------------------------------------
# Infrastructure bring-up
# ---------------------------------------------------------------------
def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def op_http(method: str, path: str, payload=None, token: str = "",
            svc: str = "") -> "tuple[int, dict]":
    """Operator-side HTTP call (L0 static token, or none for /health)."""
    url = svc + path
    data = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body}



# ---------------------------------------------------------------------------
# Zombie defence for the locally-launched helpers (fake sink + navidrome
# service). Runs of this matrix spawn REAL processes per launch; a run
# interrupted mid-life (iteration budget, SIGKILL of the harness, OOM)
# leaves them orphaned. Worse than wasting memory: a wedged service spins
# its event loop and eats a whole CPU core PER ORPHAN — on this small LXC
# six orphans were enough to wedge Chromium (and the LIVE service) for
# minutes at a time, which masquerades as "flaky bridges". Same scheme as
# the Chrome defence in cdp.py: every launch gets its own session (pgid),
# a marker file records the pgid, teardown kills THE GROUP, and each new
# launch sweeps marker groups whose owner is gone.
import signal as _sig
_SVC_PGID_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                             "nvra-svc-pgids")

# Zombie defence for the locally-launched helpers (fake sink + service).
# An interrupted run (killed harness, OOM, iteration budget) leaves them
# orphaned; a wedged service spins an event loop and eats a whole core
# PER ORPHAN — on this small LXC that wedged Chromium for minutes and
# looked like 'bridge flakiness'. Same scheme as the Chrome defence in
# cdp.py: own session (pgid), marker file, group-kill at teardown, and a
# pre-launch sweep of marker groups whose owner process is gone.

def _svc_group_marker(pid):
    os.makedirs(_SVC_PGID_DIR, exist_ok=True)
    p = os.path.join(_SVC_PGID_DIR, "%d.pgid" % pid)
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpgid(pid)))
        return p
    except OSError:
        return None

def _kill_group_by_marker(marker):
    if not marker:
        return
    try:
        with open(marker, encoding="utf-8") as fh:
            pgid = int(fh.read().strip() or "0")
        if pgid:
            try:
                os.killpg(pgid, _sig.SIGTERM)
            except OSError:
                pass
            try:
                os.killpg(pgid, _sig.SIGKILL)
            except OSError:
                pass
    except (ValueError, OSError):
        pass
    try:
        os.remove(marker)
    except OSError:
        pass

def _sweep_stale_service_groups():
    swept = 0
    try:
        entries = os.listdir(_SVC_PGID_DIR)
    except OSError:
        return 0
    for name in entries:
        mk = os.path.join(_SVC_PGID_DIR, name)
        try:
            owner_pid = int(name.rsplit(".", 1)[0])
            try:
                os.kill(owner_pid, 0)
                continue  # live owner: its group is still its business
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
            with open(mk, encoding="utf-8") as fh:
                pgid = int(fh.read().strip() or "0")
            if pgid:
                try:
                    os.killpg(pgid, _sig.SIGKILL)
                    swept += 1
                except OSError:
                    pass
            os.remove(mk)
        except (ValueError, OSError):
            continue
    return swept

class Rig:
    """Everything the scenarios need: handles + state."""

    def __init__(self, tmp: str, report_dir: str, svc_port: int,
                 up: FakeUpstream, op_token: str):
        self.tmp = tmp
        self.report_dir = report_dir
        self.svc_port = svc_port
        self.svc = f"http://127.0.0.1:{svc_port}"
        self.up = up
        self.op_token = op_token
        self.sink_proc = None
        self.svc_proc = None
        self.sink_marker = None
        self.svc_marker = None
        self.drv: "cdp.BrowserDriver | None" = None
        self.tab: "cdp.Tab | None" = None
        self.spy_path = os.path.join(tmp, "spy.ndjson")
        self.state_path = os.path.join(tmp, "state.json")
        self.sock_path = os.path.join(tmp, "mock-ipc.sock")

    # ------------------------------------------------------------------ up
    async def start_fake_upstream(self) -> None:
        await self.up.start()

    def start_engine_mock(self) -> None:
        env = os.environ.copy()
        # Trace every IPC command the fake engine receives, in order, to
        # a side file: the definitive witness for transport-path disputes
        # (e.g. whether a /next ever reached the engine as playlist-next).
        env["NVRA_SINK_LOG"] = os.path.join(self.tmp, "sink-cmds.ndjson")
        # stdout/stderr go to a FILE, never to an unread PIPE: a pipe
        # nobody drains fills its 64 kB buffer and parks the sink
        # mid-run (observed: service logging "motor FORA DO AR" 5 s in).
        sink_log = open(os.path.join(self.tmp, "sink-stdout.log"), "wb")
        self.sink_proc = subprocess.Popen(
            [sys.executable, FAKE_MPV, "--socket", self.sock_path,
             "--seed", "7", "--duration", "180"],
            stdout=sink_log, stderr=subprocess.STDOUT, env=env,
            cwd=SRC_DIR, start_new_session=True,
        )
        self._sink_log_fh = sink_log
        self.sink_marker = _svc_group_marker(self.sink_proc.pid)

    def start_service(self) -> None:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": SRC_DIR,
            "HOME": os.environ.get("HOME", "/root"),
            "NVRA_LISTEN_ADDR": "127.0.0.1",
            "NVRA_LISTEN_PORT": str(self.svc_port),
            "NVRA_UPSTREAM": f"127.0.0.1:{self.up.port}",
            "NVRA_MPV_SOCKET": self.sock_path,
            "NVRA_SPY_FILE": self.spy_path,
            "NVRA_STATE_FILE": self.state_path,
            "NVRA_API_TOKEN": self.op_token,
            "NVRA_NAV_USER": SBC_SERVICE_USER,
            "NVRA_NAV_PASS": SBC_SERVICE_PASS,
        }
        _swept = _sweep_stale_service_groups()
        if _swept:
            print("[svc] varreu grupo(s) de processo órfão(s) anterior(es): %d"
                  % _swept, file=sys.stderr)
        self.svc_proc = subprocess.Popen(
            [sys.executable, "-m", "navidrome_remote"],
            stdout=open(os.path.join(self.tmp, "svc.log"), "wb"),
            stderr=subprocess.STDOUT, env=env, cwd=SRC_DIR,
            start_new_session=True,
        )
        self.svc_marker = _svc_group_marker(self.svc_proc.pid)

    async def teardown(self) -> None:
        for proc, marker_attr in ((getattr(self, "svc_proc", None), "svc_marker"),
                                   (getattr(self, "sink_proc", None),
                                    "sink_marker")):
            if not proc:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            mk = getattr(self, marker_attr, None)
            if mk:
                # Kill the WHOLE group: grandchildren of the helper
                # (threads aside, any re-spawned child) die with it.
                try:
                    with open(mk, encoding="utf-8") as fh:
                        pgid = int(fh.read().strip() or "0")
                    if pgid:
                        try:
                            os.killpg(pgid, _sig.SIGTERM)
                        except OSError:
                            pass
                        try:
                            os.killpg(pgid, _sig.SIGKILL)
                        except OSError:
                            pass
                except (ValueError, OSError):
                    pass
                try:
                    os.remove(mk)
                except OSError:
                    pass
        if getattr(self, "drv", None) is not None:
            try:
                self.drv.shutdown()
            except Exception:
                pass
        if getattr(self, "tab", None) is not None:
            try:
                await self.tab.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------ shortcuts
    def op(self, method: str, path: str, payload=None, token=None) -> "tuple[int, dict]":
        # Retry-on-stall: this LXC intermittently makes the service answer
        # in 4-6 s under Chromium load spikes, which trips op_http's
        # 10 s urlopen timeout MID-GESTURE (observed: T14's seed queue).
        # Only IDEMPOTENT calls are retried — reads and deck reseeds.
        # Mutations like /next are NEVER retried blindly: a second hop
        # after a slow-but-successful first one would double-advance
        # the engine and corrupt the very state the scenario asserts.
        attempts = 3 if (method == "GET" or path.rstrip("/").endswith("queue")) else 1
        for i in range(attempts):
            try:
                return op_http(method, path, payload,
                               token=token or self.op_token, svc=self.svc)
            except (urllib.error.URLError, TimeoutError, OSError):
                if i == attempts - 1:
                    raise
                time.sleep(1.5)

    def spy_events(self) -> "list[dict]":
        out = []
        try:
            with open(self.spy_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append({"malformed": line[:200]})
        except FileNotFoundError:
            pass
        return out

    def api_requests(self) -> "list[dict]":
        """Requests made BY THE PAGE to /api/v1/* (CDP-collected)."""
        if self.tab is None:
            return []
        out = []
        for r in self.tab.all_requests():
            u = r.get("url", "")
            if "/api/v1/" in u:
                rr = dict(r)
                rid = r.get("requestId") or r.get("id")
                if rid and rid in self.tab.responses:
                    rr["status"] = self.tab.responses[rid].get("status")
                out.append(rr)
        return out

    def stream_requests(self) -> "list[dict]":
        """Page requests hitting /rest/stream (should be ZERO while
        engaged — the bridge silences them; non-empty after degradation
        or in the drift/native fixtures)."""
        if self.tab is None:
            return []
        return [r for r in self.tab.all_requests()
                if "/rest/stream" in r.get("url", "")]


# ---------------------------------------------------------------------
# Eval helpers (all bridge inspection returns PARSED JSON)
# ---------------------------------------------------------------------
BRIDGE_PROBE_JS = """(function(){var b=window.__NVRA_BRIDGE__;
if(!b){return JSON.stringify({present:false});}
var t=b._t||{};
function g(k){var v=t[k];return typeof v==='function'?v():v;}
return JSON.stringify({present:true,
state:g('_state'),
reason:g('_reason'),
intents:g('_intents'),
status:g('_status'),
stats:g('_stats'),
shadow:g('_shadow')});
})()
"""


FIX_PROBE_JS = "(function(){return window.__FIX__ ? __FIX__.state() : 'nofix';})()"


async def jv_async(expr: str) -> "object":
    return await TAB_Globals.tab.eval(expr)


TAB_Globals = type("TG", (), {})()  # filled in main()

async def jv(expr: str) -> "object":
    raw = await TAB_Globals.tab.eval(expr)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


async def bridge_state(rig: Rig) -> dict:
    return await jv(BRIDGE_PROBE_JS) or {}


async def fix_state(rig: Rig) -> dict:
    s = await jv(FIX_PROBE_JS)
    return s if isinstance(s, dict) else {"raw": s}


async def wait_until(pred, *, timeout_s: float = 10.0, poll_s: float = 0.2) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            if await pred():
                return True
        except Exception:
            pass
        await asyncio.sleep(poll_s)
    return False


async def rig_wait_status(rig: Rig, **want) -> bool:
    # `timeout_s` is consumed by the waiter, NOT a status field: popping it
    # keeps the **want spelling ergonomic (callers pass
    # rig_wait_status(rig, **{"paused": True}, timeout_s=15)) and stops a
    # bogus predicate (body["timeout_s"]) from failing a healthy run.
    timeout_s = float(want.pop("timeout_s", 10.0))
    async def _p() -> bool:
        st, body = rig.op("GET", "/api/v1/status")
        if st != 200:
            return False
        for k, v in want.items():
            node = body
            for part in k.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            if node != v:
                return False
        return True

    return await wait_until(_p, timeout_s=timeout_s)


def intent_seen(route: str):
    """Sync predicate builder: is `route` among the bridge's last intents?"""
    def _cond(b: dict) -> bool:
        return any((i or {}).get("route") == route
                   for i in (b.get("intents") or []))
    return _cond


async def wait_bridge(rig: Rig, cond, *, timeout_s: float = 10.0,
                      poll_s: float = 0.2) -> bool:
    async def _p() -> bool:
        return bool(cond(await bridge_state(rig)))
    return await wait_until(_p, timeout_s=timeout_s, poll_s=poll_s)


async def wait_fix(rig: Rig, cond, *, timeout_s: float = 10.0,
                   poll_s: float = 0.2) -> bool:
    async def _p() -> bool:
        return bool(cond(await fix_state(rig)))
    return await wait_until(_p, timeout_s=timeout_s, poll_s=poll_s)


async def wait_intent(rig: Rig, route: str, since_page_ms: int,
                      want: "dict | None" = None,
                      timeout_s: float = 25.0) -> bool:
    """Await a FRESH intent-ring entry for `route` (t > since_page_ms),
    optionally carrying the expected payload fields.

    A plain "route in ring" condition can be satisfied by a STALE
    intent recorded before the action under test (the T8/T9 race: the
    previous toggle's entry is still the ring's tail when the new one
    has not been filed yet). The page-clock watermark sampled right
    before the simulated gesture separates the two."""
    def _cond(b: dict) -> bool:
        for i in (b.get("intents") or []):
            i = i or {}
            if i.get("route") != route:
                continue
            if (i.get("t") or 0) <= since_page_ms:
                continue
            if want is not None:
                pl = i.get("payload") or {}
                if any(pl.get(k) != v for k, v in want.items()):
                    return False
            return True
        return False
    return await wait_bridge(rig, _cond, timeout_s=timeout_s)


async def wait_stable(rig: Rig, cond, *, samples: int = 3,
                      gap_s: float = 0.35, timeout_s: float = 15.0) -> bool:
    """Require `samples` CONSECUTIVE readings (taken `gap_s`s apart) to
    satisfy `cond`.

    A single reading is unsafe around a track swap: between the `src`
    assignment and play() taking effect, the media element transiently
    reports unstable states (one `paused` frame can flip false during the
    load window). A user click landing on that window makes the UI toggle
    pick the wrong branch (play() instead of pause()) — a fixture
    behaviour, not a bridge defect. The harness therefore demands a
    settled steady state before simulating the next user action."""
    good = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        s = await fix_state(rig)
        if s is not None and cond(s):
            good += 1
            if good >= samples:
                return True
        else:
            good = 0
        await asyncio.sleep(gap_s)
    return False


# ---------------------------------------------------------------------
# Scenario skeleton
# ---------------------------------------------------------------------
class Case:
    def __init__(self, tid: str, name: str):
        self.tid = tid
        self.name = name
        self.checks: "list[tuple[str, bool, str]]" = []
        self.notes: "list[str]" = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks.append((label, bool(ok), detail))

    def note(self, s: str) -> None:
        self.notes.append(s)

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks) and self.checks != []


# ---------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------
async def t1_passive_prelogin(rig: Rig) -> Case:
    c = Case("T1", "passive/pre-login: zero /api/v1/* traffic, no badge")
    await rig.tab.navigate(f"{rig.svc}/fixtures/anon.html", timeout=15)
    rig.tab.mark()
    await asyncio.sleep(2.0)  # give the session watcher time to (not) act
    bs = await bridge_state(rig)
    fx = await fix_state(rig)
    c.check("bridge present (injected by proxy)", bs.get("present") is True,
            str(bs)[:160])
    c.check("state == passive", bs.get("state") == "passive", str(bs.get("state")))
    reqs = rig.api_requests()
    c.check("ZERO /api/v1/* requests while anonymous", reqs == [],
            f"{len(reqs)} requests: {[r['url'] for r in reqs][:5]}")
    c.check("no badge on the login surface", fx.get("badge") is False,
            str(fx.get("badge")))
    return c


async def t2_engage_and_play(rig: Rig) -> Case:
    c = Case("T2", "engage on session + play -> /api/v1/play (silent feed)")
    await rig.tab.navigate(f"{rig.svc}/fixtures/main.html", timeout=15)
    rig.tab.mark()
    ok = await wait_bridge(rig, lambda b: b.get("state") == "engaged", timeout_s=15)
    c.check("state == engaged (session watcher)", ok,
            str((await bridge_state(rig)).get("state")))
    await jv("__FIX__.pressPlay()")
    ok = await wait_bridge(rig, intent_seen("/play"), timeout_s=6)
    bs = await bridge_state(rig)
    play = [i for i in (bs.get("intents") or []) if i.get("route") == "/play"]
    c.check("/play intent emitted with the song id",
            ok and play and play[-1].get("payload", {}).get("id") == "S1",
            str(play[-1] if play else None))
    # F6e — bounded convergence wait: the service ACKs /play BEFORE its
    # fire-and-forget IPC reaches the engine (under bed load the forward
    # can take >1 s — measured: a loadfile landing 2.2 s after the ACK).
    # A single immediate /status read then saw a half-booted engine and
    # failed a HEALTHY run. The acceptance criterion is UNCHANGED — the
    # engine must actually end up playing S1 — only the observation stops
    # assuming an instantaneous transport.
    st, body = await _wait_track(rig, "S1", timeout_s=20)
    playb = body.get("play") or {}
    # NOTE: the engine's media-title property is opaque metadata (the
    # registered fake sink reports the literal "stream"; a real mpv the
    # track's real name) — the ACCEPTANCE is the id, never the title
    # wording. Title presence is kept as a shape check.
    c.check("engine actually playing S1 (via mock mpv)",
            st == 200 and playb.get("id") == "S1"
            and playb.get("title") is not None
            and playb.get("pos") is not None and playb.get("dur") is not None,
            str(body)[:200])
    c.check("engine reports playing=true", body.get("playing") is True,
            str(body.get("playing")))
    fx = await fix_state(rig)
    cur = fx.get("cur", "")
    c.check("page media element runs the SILENT feed (data:wav), not the "
            "upstream stream", cur.startswith("data:audio/wav"), cur)
    c.check("element is muted (user hears nothing locally)",
            fx.get("muted") is True, str(fx.get("muted")))
    reqs = rig.stream_requests()
    c.check("NO /rest/stream request from the page while engaged "
            "(A6 acceptance: no upstream consumption)", reqs == [],
            f"{len(reqs)}: {[r['url'][:80] for r in reqs][:3]}")
    c.check("no degradation badge while healthy", fx.get("badge") is False,
            str(fx.get("badge")))
    return c


async def t3_pause(rig: Rig) -> Case:
    c = Case("T3", "pause button -> /pause (engine state flips)")
    # The fixture's button TOGGLES on the element's current state. Give the
    # element a settled PLAYING state first, so this click is unambiguously
    # the user's PAUSE gesture (mirrors a human clicking after the track
    # audibly starts — never within microseconds of the load).
    await wait_stable(rig, lambda f: f.get("paused") is False)
    rig.tab.mark()
    await jv("__FIX__.pressPlay()")  # toggle: playing -> pause
    bs = await wait_bridge(rig, intent_seen("/pause"), timeout_s=6)
    # F6e — same convergence semantics as T2 (see there): the /pause
    # ACK does not imply the engine has APPLIED the pause yet.
    conv = await rig_wait_status(rig, **{"paused": True, "playing": False},
                                 timeout_s=15)
    st, body = rig.op("GET", "/api/v1/status")
    c.check("/pause emitted", bool(bs), str([i for i in (await bridge_state(rig)).get("intents") or [] if i.get("route") == "/pause"]))
    c.check("engine paused", conv and st == 200 and body.get("paused") is True
            and body.get("playing") is False, str(body)[:160])
    fx = await fix_state(rig)
    c.check("UI element paused in lockstep", fx.get("paused") is True,
            str(fx.get("paused")))
    return c


async def t4_resume(rig: Rig) -> Case:
    c = Case("T4", "resume button -> /resume")
    # symmetric to T3: the button toggles on element state — wait for the
    # settled PAUSED state so this click is unambiguously the user's RESUME.
    await wait_stable(rig, lambda f: f.get("paused") is True)
    rig.tab.mark()
    await jv("__FIX__.pressPlay()")  # paused -> play
    ok = await wait_bridge(rig, intent_seen("/resume"), timeout_s=6)
    # F6e — same convergence semantics as T3 (see there).
    conv = await rig_wait_status(rig, **{"playing": True}, timeout_s=15)
    st, body = rig.op("GET", "/api/v1/status")
    c.check("/resume emitted", ok, str([(i) for i in (await bridge_state(rig)).get("intents") or [] if i.get("route") == "/resume"]))
    c.check("engine playing again", conv and body.get("playing") is True,
            str(body)[:160])
    return c


async def t5_seek(rig: Rig) -> Case:
    c = Case("T5", "seek slider -> /seek {pos}")
    rig.tab.mark()
    await jv("__FIX__.setSeek(64)")
    ok = await wait_bridge(rig, intent_seen("/seek"), timeout_s=6)
    st, body = rig.op("GET", "/api/v1/status")
    pos = (body.get("play") or {}).get("pos")
    c.check("/seek emitted", ok, str([i for i in (await bridge_state(rig)).get("intents") or [] if i.get("route") == "/seek"]))
    c.check("engine position ~= 64", st == 200 and pos is not None
            and abs(pos - 64) < 1.5, str(pos))
    return c


async def t6_volume(rig: Rig) -> Case:
    c = Case("T6", "volume slider -> /volume {level}")
    rig.mark() if hasattr(rig, "mark") else rig.tab.mark()
    await jv("__FIX__.setVol(37)")
    ok = await wait_bridge(rig, intent_seen("/volume"), timeout_s=6)
    st, body = rig.op("GET", "/api/v1/status")
    c.check("/volume emitted", ok, str([i for i in (await bridge_state(rig)).get("intents") or [] if i.get("route") == "/volume"]))
    c.check("engine volume == 37", st == 200 and body.get("volume") == 37,
            str(body.get("volume")))
    fx = await fix_state(rig)
    c.check("UI volume control projected back (37%)",
            abs(float(fx.get("volVal", 0)) - 37) < 1.0, str(fx.get("volVal")))
    return c


async def _wait_track(rig: Rig, want_id: str,
                     timeout_s: float = 12.0) -> "tuple[int, dict]":
    # Poll the service until the engine's active track is `want_id`.
    # A single read right after the intent is unsafe: a loadfile swap
    # leaves a short "stopped" window in which /status reports no active
    # track (and transport POSTs come back 503 engine-idle). What we
    # measure is the bridge decision, not engine load jitter.
    st, body = 0, {}
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st, body = rig.op("GET", "/api/v1/status")
        if st == 200 and (body.get("play") or {}).get("id") == want_id:
            return st, body
        await asyncio.sleep(0.4)
    return st, body

async def _seed_queue(rig: Rig) -> None:
    items = [{"id": i, "title": "Song " + n}
             for i, n in zip(TRACK_IDS, ("One", "Two", "Three"))]
    st, body = rig.op("POST", "/api/v1/queue",
                      {"items": items, "mode": "replace"})
    if st != 200:
        raise RuntimeError("queue seed failed: %d %s" % (st, body))


async def t7_next_prev(rig: Rig) -> Case:
    c = Case("T7", "next/prev with a seeded playlist -> /next,/prev "
                  "(single-track fallback -> /play {id})")
    # Self-contained preamble (A6): the matrix MAY run T7 alone
    # (--scenario T7). The session+engagement it presupposes normally
    # lingers from T2; establish it when absent. In a full-matrix run
    # the bridge is already engaged and this is a no-op.
    if ((await bridge_state(rig)) or {}).get("state") != "engaged":
        await rig.tab.navigate(f"{rig.svc}/fixtures/main.html", timeout=15)
        if not await wait_bridge(rig,
                                 lambda b: b.get("state") == "engaged",
                                 timeout_s=15):
            c.check("bridge engaged with the fixture session", False,
                    str(await bridge_state(rig))[:200])
            return c
    await _seed_queue(rig)
    st, body = rig.op("GET", "/api/v1/status")
    c.check("queue visible (3 items)", (body.get("queue") or {}).get("size") == 3
            or (body.get("queue") or {}).get("length") == 3,
            str(body.get("queue")))
    # Wait until the BRIDGE itself has acknowledged the seeded queue
    # (through its 1 s poll) BEFORE the gesture: the click's fresh-read
    # decision must face an ACKNOWLEDGED state. Pressing within
    # milliseconds of the operator push would race the operator's own
    # in-flight POST (a test artifact — in production a seeded queue
    # sits for seconds before a user clicks, and the fresh read covers
    # the real window: a click landing between two poll ticks).
    async def _qack() -> bool:
        st2 = await bridge_state(rig) or {}
        q = (st2.get("status") or {}).get("queue") or {}
        return q.get("size") == 3
    acked = await wait_until(_qack, timeout_s=30)
    _bs_ack = await bridge_state(rig) or {}
    _q_ack = (_bs_ack.get("status") or {}).get("queue")
    c.check("bridge acknowledged the 3-item queue (pre-gesture)",
            acked, str(_q_ack))
    if not acked:
        return c
    # Deterministic starting point: park the fixture on S1 (the engine's
    # current track — the bridge's same-id echo guard swallows this swap,
    # no /play ships) and suspend the fixture's 'ended' auto-advance, so
    # each click below is the sole source of a src swap.
    await jv("__FIX__.holdEnded(true); __FIX__.prime('S1');")
    await asyncio.sleep(0.4)
    pn = await jv("Date.now()")
    rig.tab.mark()
    await jv("__FIX__.pressNext()")
    ok = await wait_intent(rig, "/next", pn, timeout_s=25)
    c.check("/next emitted", ok,
            str([i for i in (await bridge_state(rig)).get("intents") or []
                 if i.get("route") == "/next"]))
    st, body = await _wait_track(rig, "S2")
    c.check("engine advanced to S2",
            st == 200 and (body.get("play") or {}).get("id") == "S2",
            str(body.get("play")))
    pn2 = await jv("Date.now()")
    rig.tab.mark()
    await jv("__FIX__.pressPrev()")
    ok = await wait_intent(rig, "/prev", pn2, timeout_s=25)
    c.check("/prev emitted", ok,
            str([i for i in (await bridge_state(rig)).get("intents") or []
                 if i.get("route") == "/prev"]))
    st, body = await _wait_track(rig, "S1")
    c.check("engine rewound to S1",
            st == 200 and (body.get("play") or {}).get("id") == "S1",
            str(body.get("play")))
    await jv("__FIX__.holdEnded(false)")
    return c

async def t8_shuffle(rig: Rig) -> Case:
    c = Case("T8", "shuffle toggle -> /shuffle {value}")
    await _seed_queue(rig)
    pn = await jv("Date.now()")
    rig.tab.mark()
    await jv("__FIX__.pressShuffle()")  # OFF -> ON
    ok = await wait_intent(rig, "/shuffle", pn, {"value": True}, timeout_s=25)
    bs = await bridge_state(rig)
    shuf = [i for i in (bs.get("intents") or []) if i.get("route") == "/shuffle"]
    c.check("/shuffle emitted (value=true)",
            ok and shuf
            and shuf[-1].get("payload", {}).get("value") is True,
            str(shuf[-1] if shuf else None))
    st, body = rig.op("GET", "/api/v1/status")
    c.check("engine shuffle ON", body.get("shuffle") is True,
            str(body.get("shuffle")))
    pn2 = await jv("Date.now()")
    await jv("__FIX__.pressShuffle()")  # ON -> OFF
    ok2 = await wait_intent(rig, "/shuffle", pn2, {"value": False}, timeout_s=25)
    st, body = rig.op("GET", "/api/v1/status")
    c.check("second toggle turns shuffle OFF",
            ok2 and body.get("shuffle") is False,
            str(body.get("shuffle")))
    return c


async def t9_repeat_cycle(rig: Rig) -> Case:
    c = Case("T9", "repeat icon cycle -> /repeat {off|playlist|track}")
    pn = await jv("Date.now()")
    rig.tab.mark()
    await jv("__FIX__.pressRepeat()")  # rep-off -> rep-all (=playlist)
    ok = await wait_intent(rig, "/repeat", pn, {"mode": "playlist"}, timeout_s=25)
    bs = await bridge_state(rig)
    rp = [i for i in (bs.get("intents") or []) if i.get("route") == "/repeat"]
    st, body = rig.op("GET", "/api/v1/status")
    c.check("/repeat emitted with mode=playlist",
            ok and rp and rp[-1].get("payload", {}).get("mode") == "playlist",
            str(rp[-1] if rp else None))
    c.check("engine repeat == playlist", body.get("repeat") == "playlist",
            str(body.get("repeat")))
    pn2 = await jv("Date.now()")
    await jv("__FIX__.pressRepeat()")  # rep-all -> rep-one (=track)
    ok2 = await wait_intent(rig, "/repeat", pn2, {"mode": "track"}, timeout_s=25)
    st, body = rig.op("GET", "/api/v1/status")
    c.check("second step emits mode=track", ok2,
            str(body.get("repeat")))
    c.check("engine repeat == track (single-song loop)",
            body.get("repeat") == "track", str(body.get("repeat")))
    pn3 = await jv("Date.now()")
    await jv("__FIX__.pressRepeat()")  # rep-one -> rep-off
    ok3 = await wait_intent(rig, "/repeat", pn3, {"mode": "off"}, timeout_s=25)

    async def _off() -> bool:
        return rig.op("GET", "/api/v1/status")[1].get("repeat") == "off"
    okst = await wait_until(_off, timeout_s=4)
    c.check("third step returns to off", ok3 and okst,
            "wait_intent=" + str(ok3) + " engine-off=" + str(okst))
    return c



async def t10_stop(rig: Rig) -> Case:
    """Stop propagates to the UI player: engine converges (F6e) AND
    the element's own projection lands WITHIN the poll horizon (A6.R
    T10). The old single immediate read after the converged status
    raced the 1s poll cycle and flaked 1/9: a pre-stop poll could
    UNPAUSE the element after the fixture's own local pause, and with
    no stop-projection branch in the bridge NOTHING re-set it — the
    silence feed then kept "playing" forever."""
    c = Case("T10", "stop -> engine idle, UI stops too")
    # PRELUDE (solo-run independent): the engine must hold an ACTIVE
    # track. In the full matrix T9 leaves it running; in a
    # --scenario T10 solo run the engine is fresh (idle) and a /stop
    # would die as 503 engine-idle (discarded per ADR-004 — no ring
    # entry). Making the contract explicit instead of accidental.
    await _seed_queue(rig)

    async def _eng_active() -> bool:
        return rig.op("GET", "/api/v1/status")[1].get("playing") is True
    await wait_until(_eng_active, timeout_s=15)
    rig.tab.mark()
    await jv("__FIX__.pressStop()")
    ok = await wait_bridge(rig, intent_seen("/stop"), timeout_s=6)
    # F6e — same convergence semantics as T3 (see there): the stop must
    # propagate through the engine before the idle proof reads cleanly.
    conv = await rig_wait_status(rig, **{"idle": True, "play": None},
                                  timeout_s=15)
    st, body = rig.op("GET", "/api/v1/status")
    c.check("/stop emitted", ok, str([i for i in (await bridge_state(rig)).get("intents") or [] if i.get("route") == "/stop"]))
    c.check("engine idle / no active track",
            conv and body.get("idle") is True and body.get("play") is None,
            str(body)[:160])
    # A6.R (T10) — wait for the ELEMENT to converge to its own stop
    # within the poll horizon (<= ~2 cycles of 1s), instead of one
    # instant read: this is exactly the 1/9 race inverted.
    oks = await wait_fix(rig, lambda f: f.get("paused") is True,
                          timeout_s=12)
    c.check("UI element stopped in lockstep", oks,
            str((await fix_state(rig)).get("paused")))
    return c


async def t11_reverse_sync(rig: Rig) -> Case:
    c = Case("T11", "engine->UI reverse sync: seek/volume/current-track/"
                    "pause (acceptance: 'the playback state changes "
                    "according to the command')")
    # prelude: the engine must hold an ACTIVE track for transport commands
    # to act on (the prior case stopped it — operator /play restores it).
    await _seed_queue(rig)
    st, body = rig.op("POST", "/api/v1/play", {"id": "S1"})

    async def _engine_playing() -> bool:
        return rig.op("GET", "/api/v1/status")[1].get("playing") is True

    await wait_until(_engine_playing, timeout_s=8)
    c.check("operator /play S1 reactivated the engine",
            st == 200, str(body))

    # a) operator seeks the ENGINE; the UI must follow.
    st, body = rig.op("POST", "/api/v1/seek", {"pos": 99})
    c.check("operator /seek accepted", st == 200, str(body))
    fx = await wait_until(lambda: _fx_seek_near(99), timeout_s=4)
    c.check("seek slider jumped to ~99", fx, str((await fix_state(rig)).get("seekVal")))

    # b) operator volume; the UI slider follows.
    st, body = rig.op("POST", "/api/v1/volume", {"level": 80})
    c.check("operator /volume accepted", st == 200, str(body))
    ok = await wait_fix(rig, lambda f: abs(float(f.get("volVal", 0)) - 80) < 1.0, timeout_s=4)
    c.check("volume slider followed the engine (80)", ok,
            str((await fix_state(rig)).get("volVal")))

    # c) engine-driven track change: the bridge must RETRIGGER the native
    #    track-switch pipeline (no /play from the page!) and the "Now
    #    Playing" label must follow the ENGINE's current id.
    rig.tab.mark()
    st, body = rig.op("POST", "/api/v1/play", {"id": "S3"})
    c.check("operator /play S3 accepted", st == 200, str(body))
    ok = await wait_fix(rig, lambda f: f.get("syncId") == "S3", timeout_s=6)
    fx = await fix_state(rig)
    # WINDOW: only what crossed AFTER the operator push (the tab carries
    # the whole session history — the earlier cases' own /play calls are
    # legitimate user intents, not evidence against this path).
    page_api = rig.tab.requests_since_mark()
    page_play = [r for r in page_api
                 if r.get("url", "").endswith("/api/v1/play")]
    c.check("'Now Playing' re-triggered by the engine push (S3)", ok,
            str(fx.get("syncId")) + " np=" + str(fx.get("np")))
    c.check("NO /play request came FROM the page (engine-pushed path "
            "ships nothing)", page_play == [],
            f"{len(page_play)} page-side /play")

    # d) operator pauses; the UI element must pause in lockstep.
    st, body = rig.op("POST", "/api/v1/pause")
    ok = await wait_fix(rig, lambda f: f.get("paused") is True, timeout_s=4)
    c.check("UI paused after engine-side pause", ok,
            str((await fix_state(rig)).get("paused")))
    return c


async def _fx_seek_near(target: float) -> bool:
    fx = await fix_state(TAB_Globals.rig)
    try:
        return abs(float(fx.get("seekVal", 0)) - target) < 2.5
    except (TypeError, ValueError):
        return False


async def t12_gate_403_service_account(rig: Rig) -> Case:
    """Deterministic in-scene T12 (A6.R): with an ORDINARY session the
    bridge is first PROVEN to hold a synthetic re-trigger (engine-pushed
    id riding on the element), THEN the page swaps to the service
    account INSIDE the same scene — the very next poll presents the L2
    403 and native restoration must leave ZERO synthetic traces.
    The gate below pins the formerly interleaving-dependent ordering
    (poll with a foreign id -> 403 in the same scene), which the old
    boot-time sac.html flow could not observe at all: its first poll
    already 403'd, before any re-trigger existed to corrupt the restore
    target."""
    c = Case("T12", "gate 403 (service account) AFTER a synthetic "
                    "re-trigger: deterministic in-scene ordering + ADR-004 "
                    "native restoration with zero synthetic traces")
    await rig.tab.navigate(f"{rig.svc}/fixtures/main.html", timeout=15)
    rig.tab.mark()
    ok = await wait_bridge(rig, lambda b: b.get("state") == "engaged",
                           timeout_s=15)
    c.check("bridge engaged with the ordinary session", ok,
            str((await bridge_state(rig)).get("state")))
    if not ok:
        return c
    # PRELUDE (operator plane, deterministic): reseed the deck — the
    # queue POST starts playback at index 0 (S1) regardless of whatever
    # the previous scene left behind.
    await _seed_queue(rig)

    async def _eng_s1() -> bool:
        b = rig.op("GET", "/api/v1/status")[1]
        return b.get("playing") is True \
            and ((b.get("play") or {}).get("id") == "S1")
    ok1 = await wait_until(_eng_s1, timeout_s=20)
    c.check("engine playing S1 (prelude)", ok1,
            str(rig.op("GET", "/api/v1/status")[1])[:120])
    # The user selects S1 ON THE PAGE: the element is ARMED with the
    # REAL S1 stream URL (its future restore target); the /play it
    # ships is the SAME id the engine already holds — echo, no write.
    await jv("__FIX__.pressPlay()")
    okarm = await wait_fix(rig, lambda f: f.get("muted") is True
                           and str(f.get("cur", "")).startswith("data:"),
                           timeout_s=10)
    c.check("element armed on the real S1 url (silent feed)", okarm,
            str(await fix_state(rig))[:160])
    # EXTERNAL movement (OPERATOR): engine moves to S2. The next poll
    # carries a FOREIGN id versus the element's last swap -> the
    # engine-pushed re-trigger fires and publishes S2 on the element.
    st, _b = rig.op("POST", "/api/v1/play", {"id": "S2"})
    assert st == 200, "operator /play S2 rejected: " + str(_b)

    async def _eng_s2() -> bool:
        b = rig.op("GET", "/api/v1/status")[1]
        return b.get("playing") is True \
            and ((b.get("play") or {}).get("id") == "S2")
    ok2 = await wait_until(_eng_s2, timeout_s=20)
    c.check("engine moved to S2 (operator push)", ok2,
            str(rig.op("GET", "/api/v1/status")[1])[:120])
    # DETERMINISTIC GATE: the re-trigger must land BEFORE the 403 is
    # provoked. From this moment the restore target IS corrupted under
    # the pre-fix code (synthetic) — the scenario no longer depends on
    # interleaving luck.
    okrt = await wait_fix(rig, lambda f: f.get("syncId") == "S2",
                          timeout_s=10)
    c.check("synthetic re-trigger LANDED before the L2 403 "
            "(ordering fixed, no more dice roll)", okrt,
            "syncId=" + str((await fix_state(rig)).get("syncId")))
    # IN-SCENE SESSION FLIP: the page now presents the SBC SERVICE
    # ACCOUNT (the L2 target) — the very next poll presents it.
    await jv("localStorage.setItem('token', '" + SBC_JWT + "')")
    ok = await wait_bridge(rig, lambda b: b.get("state") == "degraded",
                           timeout_s=15)
    bs = await bridge_state(rig)
    c.check("state == degraded", ok, str(bs.get("reason")))
    c.check("degradation reason names the L2 gate code "
            "(service account)",
            (bs.get("reason") or "").startswith("gate:")
            and "service-account" in (bs.get("reason") or ""),
            str(bs.get("reason")))
    c.check("explanatory badge visible",
            (await fix_state(rig)).get("badge") is True,
            str((await fix_state(rig)).get("badge")))
    # NATIVE RESTORATION: the element must return to the REAL stream
    # URL, un-muted, with the synthetic MARK purged everywhere.
    oknat = await wait_fix(rig, lambda f:
                           str(f.get("cur", "")).startswith("http://")
                           and f.get("muted") is False, timeout_s=10)
    fx = await fix_state(rig)
    cur = fx.get("cur", "")
    # currentSrc is ABSOLUTE in browsers (scheme+host prefixed) — the
    # check tolerates the real stream URL in either shape and only
    # requires that it be the ORIGINAL (not the silent/synthetic feed).
    is_native = ("data:" not in cur[:5] and "__nvr_synth" not in cur
                 and "/rest/stream" in cur
                 and re.search(r"[?&]id=", cur) is not None)
    c.check("NATIVE restoration: element plays the real stream URL again "
            "(un-muted, local audio resumes)",
            oknat and is_native,
            f"cur={cur[:90]} muted={fx.get('muted')}")
    c.check("NO synthetic residue: data-nvr-sync-id PURGED on "
            "restoration (ADR-004: restoration is complete; the mark "
            "was ours)",
            (fx.get("syncId") in (None, "")),
            "syncId=" + str(fx.get("syncId")))
    reqs = rig.stream_requests()
    c.check("the restored stream WAS fetched from the (fake) upstream "
            "(native mode is genuinely playable)", len(reqs) >= 1,
            f"{len(reqs)} stream requests")
    evs = [e for e in rig.spy_events()
           if e.get("event") in ("gate_deny_service_account",)]
    c.check("spy recorded the L2 deny (audit evidence)", len(evs) >= 1,
            str(evs[:1]))
    return c


async def t13_anchor_drift(rig: Rig) -> Case:
    c = Case("T13", "anchor drift (UI v0.65.0 vs SBC v0.64.2): refuse to "
                    "touch; native playback stays 100% functional")
    await rig.tab.navigate(f"{rig.svc}/fixtures/drift.html", timeout=15)
    rig.tab.mark()
    ok = await wait_bridge(rig, lambda b: b.get("state") == "degraded", timeout_s=15)
    bs = await bridge_state(rig)
    c.check("state == degraded", ok, str(bs.get("state")))
    c.check("reason == anchor-drift", (bs.get("reason") or "") == "anchor-drift",
            str(bs.get("reason")))
    # WINDOW: the tab accumulates the whole session (previous cases
    # legitimately drove /api/v1/* while engaged) — only what crossed
    # AFTER navigating to the drift page counts against the gate.
    page_reqs = rig.tab.requests_since_mark()
    # NO /api/v1/* API traffic whatsoever besides the (gated) spy pad;
    # non-API resources (the bridge.js script itself, fixture files, the
    # original stream) are intrinsic to the page and irrelevant to the
    # interception gate.
    non_spy = [r for r in page_reqs
               if str(r.get("url", "")).startswith("/api/v1/")
               and not str(r.get("url", "")).endswith("/api/v1/spy")]
    c.check("NO /api/v1/* intent/status traffic (version gate refuses "
            "interception)", non_spy == [],
            f"{len(non_spy)}: {[r['url'] for r in non_spy][:5]}")
    # the page must still WORK natively: press play -> real stream loads
    await jv("__FIX__.pressPlay()")
    await asyncio.sleep(1.5)
    fx = await fix_state(rig)
    cur = fx.get("cur", "")
    is_native = ("data:" not in cur[:5] and "__nvr_synth" not in cur
                 and "/rest/stream" in cur
                 and re.search(r"[?&]id=", cur) is not None)
    c.check("native playback unaffected (real stream loaded & audible)",
            is_native and fx.get("muted") is False,
            f"cur={cur[:90]} muted={fx.get('muted')}")
    return c


async def t14_recovery_reauth(rig: Rig) -> Case:
    c = Case("T14", "re-auth: a NEW session (new page) engages again and "
                    "controls the engine")
    # the user's real flow after the T12 denial: re-login -> land in the
    # (fresh) app page with the good session.
    await rig.tab.navigate(f"{rig.svc}/fixtures/main.html", timeout=15)
    rig.tab.mark()
    ok = await wait_bridge(rig, lambda b: b.get("state") == "engaged", timeout_s=15)
    c.check("bridge re-engaged with the ordinary session", ok,
            str((await bridge_state(rig)).get("state")))
    await _seed_queue(rig)
    await jv("__FIX__.pressPlay()")
    ok = await wait_fix(rig, lambda f: f.get("np") in TRACK_IDS and f.get("paused") is False, timeout_s=8)
    st, body = rig.op("GET", "/api/v1/status")
    c.check("engine playing again after re-auth",
            body.get("playing") is True, str(body)[:160])
    c.check("UI shows the active track", ok,
            str((await fix_state(rig)).get("np")))
    return c


async def t15_invariants_scan(rig: Rig) -> Case:
    c = Case("T15", "credential invariant + no-consumption over ALL "
                    "artifacts (spy.ndjson, CDP dump, report)")
    from navidrome_remote import spy as spy_mod

    blobs: "list[tuple[str, str]]" = []
    # 1) the spy file RAW (it must already be redacted at the chokepoint)
    try:
        with open(rig.spy_path, "r", encoding="utf-8", errors="replace") as fh:
            blobs.append(("spy.ndjson", fh.read()))
    except FileNotFoundError:
        blobs.append(("spy.ndjson", ""))
    # 2) the CDP dump (requests/responses/console), redacted by the
    #    suite's own redactor (double safety for the report).
    if rig.tab is not None:
        dump = json.dumps(
            {"requests": rig.tab.all_requests(),
             "responses": {k: v for k, v in rig.tab.responses.items()},
             "console": rig.tab.console},
            ensure_ascii=False, default=str,
        )
        blobs.append(("cdp-dump", cdp.redact(dump)))
    # 3) badge texts seen so far (already collected into notes by T12)
    for note in c.notes:
        blobs.append(("badge-note", note))

    offenders = []
    for name, text in blobs:
        for s in SENTINELS:
            if s and s in text:
                offenders.append(f"{name}: sentinel {s[:12]}… leaked")
        if SENTINEL_JWT_RX.search(text):
            offenders.append(f"{name}: JWT-shaped value present")
        # short-but-named params surviving is a leak too (u/t/s/p values)
        for m in re.finditer(r"[?&](?:u|t|s|p|token|jwt)=([^&\s]{8,})",
                              text):
            val = m.group(1)
            if val == "[REDACTED]":
                continue
            if re.fullmatch(r"[0-9a-f]+", val, re.I) or val.isdigit():
                offenders.append(f"{name}: named-param value '{val[:12]}…'"
                                 " survived")
    c.check("no sentinel value in any artifact (spy, CDP dump, badges)",
            offenders == [], "; ".join(offenders[:6]))

    # the spy must show POSITIVE evidence of redaction where credentials
    # DID cross a logged path (the T12 native-restoration stream fetch
    # carries u/t/s; the fixture's s= is short — the NAME-based rule is
    # what must catch it).
    spy_raw = blobs[0][1]
    red = spy_raw.count("[REDACTED]")
    c.check("spy.redaction chokepoint demonstrably fired ([REDACTED] "
            "sentinel present in the audit trail)", red >= 1,
            f"{red} redactions in spy.ndjson")
    return c


SCENARIOS = [
    ("T1", t1_passive_prelogin),
    ("T2", t2_engage_and_play),
    ("T3", t3_pause),
    ("T4", t4_resume),
    ("T5", t5_seek),
    ("T6", t6_volume),
    ("T7", t7_next_prev),
    ("T8", t8_shuffle),
    ("T9", t9_repeat_cycle),
    ("T10", t10_stop),
    ("T11", t11_reverse_sync),
    ("T12", t12_gate_403_service_account),
    ("T13", t13_anchor_drift),
    ("T14", t14_recovery_reauth),
    ("T15", t15_invariants_scan),
]


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------
def find_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "headless-shell", "headless_shell"):
        p = shutil.which(name)
        if p:
            return p
    return None


async def _amain(args) -> int:
    tmp = os.path.join(
        args.report_dir or "/tmp",
        "bridge-a6-%d" % int(time.time()),
    )
    os.makedirs(tmp, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_root = args.report_dir or os.path.join(
        REPO_ROOT, "src", "docs", "reports")
    report_dir = os.path.join(report_root, f"a6-{stamp}-{os.path.basename(tmp)}")
    os.makedirs(report_dir, exist_ok=True)

    rig = Rig(
        tmp=tmp, report_dir=report_dir,
        svc_port=_free_port(),
        up=FakeUpstream(
            subsonic_users={FAKE_U: FAKE_T},
            subsonic_jwts=[ALICE_JWT],
            cookie_sessions={},
        ),
        op_token=os.urandom(24).hex(),
    )
    TAB_Globals.rig = rig  # type: ignore[attr-defined]

    chrome = args.chrome or find_chrome()
    if chrome is None:
        print("ERRO: nenhum Chrome/Chromium encontrado (uso: --chrome BIN). "
              "Sem navegador, a matriz browser-level não roda; os testes "
              "herméticos continuam válidos.", file=sys.stderr)
        return 2

    rig.drv = cdp.BrowserDriver(chrome_bin=chrome)
    try:
        rig.drv.launch(0)
    except Exception as exc:
        print(f"ERRO: launch do Chrome falhou: {exc}", file=sys.stderr)
        return 2

    loop = asyncio.get_running_loop()
    try:
        await rig.start_fake_upstream()
        rig.start_engine_mock()
        rig.start_service()
    except Exception:
        await rig.teardown()
        raise

    # wait for the service. With NVRA_API_TOKEN set (as here), the L0
    # precondition applies to EVERY /api/v1/* call — so probe health WITH
    # the operator token (L0 is exactly this operator-side channel).
    async def _health_ok() -> bool:
        st, _body = op_http(
            "GET", "/api/v1/health", svc=rig.svc, token=rig.op_token)
        return st == 200
    healthy = await wait_until(_health_ok, timeout_s=45, poll_s=0.5)
    if not healthy:
        with open(os.path.join(tmp, "svc.log"), "rb") as fh:
            tail = fh.read()[-4000:].decode(errors="replace")
        print("ERRO: serviço não subiu.\n" + tail, file=sys.stderr)
        await rig.teardown()
        return 1
    print(f"[bridge-e2e] serviço no ar: {rig.svc} (spy={rig.spy_path})")

    try:
        rig.tab = rig.drv.new_tab(f"{rig.svc}/fixtures/anon.html", loop)
    except Exception as exc:
        print(f"ERRO: aba CDP não conectou: {exc}", file=sys.stderr)
        await rig.teardown()
        return 1
    await rig.tab.start()
    TAB_Globals.tab = rig.tab  # type: ignore[attr-defined]
    print("[bridge-e2e] aba CDP pronta")

    wanted = {s.upper() for s in args.scenario.split(",")} if args.scenario else None
    results: "list[tuple[str, Case]]" = []
    for tid, coro_fn in SCENARIOS:
        if wanted and tid not in wanted:
            continue
        t0 = time.monotonic()
        try:
            case = await coro_fn(rig)
        except Exception:
            case = Case(tid, f"<exception> {traceback.format_exc(limit=3)}")
            case.check("scenario ran without exception", False,
                       traceback.format_exc(limit=2))
        dt = time.monotonic() - t0
        verdict = "PASS" if case.ok else "FAIL"
        print(f"  {tid} {verdict}  ({dt:.1f}s)  {case.name}")
        results.append((tid, case))
        if args.keep_going is False and not case.ok and tid in ("T2",):
            print("  (T2 fundamental falhou — interrompendo a sequência)",
                  file=sys.stderr)
            break

    # ---------------------------------------------------------------- report
    _write_report(rig, results, chrome)
    print(f"[bridge-e2e] relatório: {report_dir}")

    await rig.teardown()
    fails = [tid for tid, cs in results if not cs.ok]
    if fails:
        print(f"FALHOU: {','.join(fails)}", file=sys.stderr)
        return 1
    print(f"OK: {len(results)}/{len(SCENARIOS)} cenários passaram")
    return 0


# Precise DOC scrubber — the chokepoint the report footer promises.
# The coarse CDP_REDACTIONS tuples are sized for HEADER VALUES: applied
# to a whole document they shredded useful material (the 2048-char cap
# truncation saga; the rig line's asset sha256 rendered as
# "<b64:redigido>"). For a document we apply the JWT shape, a NAMED-
# PARAM scrub restricted to secret-shaped values (long hex/base64/digit
# runs), and the exact synthetic sentinels.
DOC_JWT_RX = re.compile(
    r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{2,}")
DOC_NAMED_RX = re.compile(
    r"([?&][A-Za-z][A-Za-z0-9_-]*=)([^&\s\"'<>()\]]+)")
DOC_SENTINELS = [ALICE_JWT, SBC_JWT, FAKE_T, FAKE_S, SBC_SERVICE_PASS]


def _secret_shape(v: str) -> bool:
    if len(v) < 8:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{8,}", v):
        return True
    if re.fullmatch(r"[A-Za-z0-9+/=]{16,}", v):
        return True
    if re.fullmatch(r"\d{4,}", v):
        return True
    return False


def _scrub_doc(t: str) -> str:
    for m in list(DOC_JWT_RX.finditer(t)):
        t = t.replace(m.group(0), "[REDACTED]")

    def _rep(mm):
        val = mm.group(2)
        if val == "[REDACTED]":
            return mm.group(0)
        return ((mm.group(1) + "[REDACTED]") if _secret_shape(val)
                else mm.group(0))
    t = DOC_NAMED_RX.sub(_rep, t)
    for sig in DOC_SENTINELS:
        if sig:
            t = t.replace(sig, "[REDACTED]")
    return t


def _write_report(rig: Rig, results, chrome: str) -> None:
    lines = [
        "# A6 — Evidence: browser-level matrix (bridge.js)",
        "",
        "- host: `%s`, chrome: `%s`" % (os.uname().nodename, chrome),
        "- rig: service `%s`, upstream `127.0.0.1:%d`, sink `%s`"
        % (rig.svc, rig.up.port, rig.sock_path),
        "- asset sha256: `%s`" % _sha256(ASSET_PATH),
        "",
        "| caso | veredito | detalhes |",
        "|---|---|---|",
    ]
    for tid, cs in results:
        v = "✅" if cs.ok else "❌"
        row_det = []
        for chk in cs.checks:
            try:
                label, ok, detail = chk[0], bool(chk[1]), str(
                    chk[2] if len(chk) > 2 else "")
                row_det.append("{" + ("✓" if ok else "✗") + "} " + label
                               + ((" — " + detail) if detail else ""))
            except Exception as exc:
                row_det.append("✗ (detail render error: %r)" % (exc,))
        det = ("<br>".join(row_det) or "-").replace("\n", " ") \
            .replace("|", "\\|")
        lines.append("| %s | %s | %s<br>%s |" % (
            tid, v, str(cs.name)[:200], _scrub_doc(det)[:4000]))
    lines += ["",
              "_Credenciais sintéticas derivadas em página; este documento "
              "passa pelo redator do espião antes da persistência._", ""]
    text = "\n".join(lines)
    # Full-document scrub BEFORE persistence (the chokepoint the footer
    # claims). The asset sha256 line survives on purpose: a hash of a
    # public committed file is not a credential, and neither rule
    # matches it outside a query context.
    text = _scrub_doc(text)
    with open(os.path.join(rig.report_dir, "report.md"), "w",
              encoding="utf-8") as fh:
        fh.write(text)
    try:
        import shutil as _sh
        _sh.copy(rig.spy_path, os.path.join(rig.report_dir, "spy.ndjson"))
    except Exception:
        pass
    if rig.tab is not None:
        raw = json.dumps(
            {"requests": rig.tab.all_requests(),
             "responses": {k: v for k, v in rig.tab.responses.items()},
             "console": rig.tab.console},
            ensure_ascii=False, indent=1, default=str)
        # the EVIDENCE file gets the same doc scrub: raw request targets
        # (u/t/s) must not survive on disk.
        with open(os.path.join(rig.report_dir, "cdp-dump.json"), "w",
                  encoding="utf-8") as fh:
            fh.write(_scrub_doc(raw))


def _sha256(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", default="",
                    help="comma list (T1,T2,...); empty = all")
    ap.add_argument("--chrome", default="",
                    help="chrome/chromium binary (default: autodetect)")
    ap.add_argument("--report-dir", default="",
                    help="report root (default: src/docs/reports)")
    ap.add_argument("--keep-going", action="store_true", default=False,
                    help="continue after a fundamental failure (default: "
                         "stop on T2)")
    args = ap.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
