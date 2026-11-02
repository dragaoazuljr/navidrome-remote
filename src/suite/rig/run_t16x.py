#!/usr/bin/env python3
"""Run the REAL T16 scenario against the instrumented (discardable)
tree and dump the __WX__ ring afterwards. Nothing here ships."""
import asyncio
import json
import os
import sys
import time

# Layout-robust rig script. Runs in BOTH trees:
#  * flat discardable copy:  <copy>/run_run_t16x.py
#                            (<copy>/suite, <copy>/navidrome_remote, <copy>/tools)
#  * nested repo checkout:   <repo>/src/suite/rig/run_run_t16x.py
# The three path-derived constants below are retargeted to whichever root
# the copy actually uses (single source of truth).
def _siblings_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    for _ in range(5):
        if all(os.path.isdir(os.path.join(d, n))
               for n in ("suite", "navidrome_remote", "tools")):
            return d
        d = os.path.dirname(d)
    sys.exit("layout nao reconhecido (esperado: suite/ + navidrome_remote/ + tools/)")

SR = _siblings_root()
os.chdir(SR)
sys.path.insert(0, SR)

from suite import bridge_e2e as be  # noqa: E402
from suite.cdp import BrowserDriver  # noqa: E402

be.SRC_DIR = SR
be.ASSET_PATH = os.path.join(
    SR, "navidrome_remote", "assets", "bridge.js")
be.FAKE_MPV = os.path.join(SR, "tools", "fake_mpv_sink.py")

WORK = "/tmp/a8x-w"
os.makedirs(WORK, exist_ok=True)


# --- Popen forensics (diagnostic only) ----------------------------
import subprocess as _sp


class _FProbe(_sp.Popen):
    def __init__(self, args, *a, **kw):
        try:
            dbg = open(os.path.join(WORK, "popen-debug.log"), "a")
            dbg.write("ARGS=%r\nCWD=%r\nENVKEYS=%s\n" % (
                list(args)[:4], kw.get("cwd"),
                sorted((kw.get("env") or {}).keys())))
            dbg.write("ENV=%.120r\n" % dict(kw.get("env") or {}))
            dbg.write("EXEC=%r PYVERFILE=?\n" % sys.executable)
            dbg.close()
        except Exception:
            pass
        super().__init__(args, *a, **kw)


_sp.Popen = _FProbe  # affects bridge_e2e's subprocess calls
# ---------------------------------------------------------------


async def amain():
    report_dir = os.path.join(WORK, "rep")
    os.makedirs(report_dir, exist_ok=True)
    print("[x-pre] be.__file__ =", be.__file__)
    print("[x-pre] SRC_DIR    =", be.SRC_DIR)
    print("[x-pre] FAKE_MPV   =", be.FAKE_MPV)
    print("[x-pre] ASSET_PATH =", be.ASSET_PATH)
    print("[x-pre] exec       =", sys.executable)
    rig = be.Rig(
        tmp=WORK, report_dir=report_dir, svc_port=be._free_port(),
        up=be.FakeUpstream(
            subsonic_users={be.FAKE_U: be.FAKE_T},
            subsonic_jwts=[be.ALICE_JWT],
            cookie_sessions={}),
        op_token=os.urandom(24).hex(),
    )
    rig.drv = BrowserDriver(chrome_bin=be.find_chrome())
    rig.drv.launch(0)
    loop = asyncio.get_running_loop()
    await rig.start_fake_upstream()
    rig.start_engine_mock()
    rig.start_service()

    async def _health_ok() -> bool:
        st, _ = be.op_http("GET", "/api/v1/health",
                           svc=rig.svc, token=rig.op_token)
        return st == 200

    healthy = await be.wait_until(_health_ok, timeout_s=45, poll_s=0.5)
    if not healthy:
        tail = open(os.path.join(WORK, "svc.log"), "rb").read()[-3000:]
        print("ERRO: serviço não subiu\n" +
              tail.decode(errors="replace"), file=sys.stderr)
        await rig.teardown()
        return 2
    print(f"[x-run] up {rig.svc}")

    rig.tab = rig.drv.new_tab("about:blank", loop)
    await rig.tab.start()
    be.TAB_Globals.rig = rig
    be.TAB_Globals.tab = rig.tab

    t0 = time.time()
    case = await be.t16_logout_mid_session(rig)
    print(f"[x-run] T16 checks ({case.tid}):")
    for label, ok, detail in case.checks:
        print(("  OK  " if ok else "  XX  ") + label
              + " | " + detail[:110])
    txt = await be.jv("JSON.stringify(window.__WX__ || [])")
    ring = json.loads(txt) if isinstance(txt, str) else []
    with open(os.path.join(WORK, "wx-ring.ndjson"), "w") as fh:
        for line in ring:
            fh.write(line + "\n")
    print(f"[x-run] ring lines: {len(ring)} -> {WORK}/wx-ring.ndjson")
    bs = await be.jv("(function(){ var s = window.__BRIDGE_STATE__; return s ? JSON.stringify(s) : ''; })()")
    print("[x-run] final state:", bs)
    await rig.teardown()
    return 0


if __name__ == "__main__":
    rc = asyncio.run(amain())
    sys.exit(rc)
