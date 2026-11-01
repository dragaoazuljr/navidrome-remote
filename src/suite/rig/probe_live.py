#!/usr/bin/env python3
"""Live-page interrogation: navigate to the instrumented fixture, wait,
invoke a tick by hand, and dump what the PAGE sees."""
import asyncio
import json
import os
import sys

# Layout-robust rig script. Runs in BOTH trees:
#  * flat discardable copy:  <copy>/run_probe_live.py
#                            (<copy>/suite, <copy>/navidrome_remote, <copy>/tools)
#  * nested repo checkout:   <repo>/src/suite/rig/run_probe_live.py
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

WORK = "/tmp/a8x-probe"
os.makedirs(WORK, exist_ok=True)



async def amain():
    import subprocess as _sp

    class _FP(_sp.Popen):
        def __init__(self, args, *a, **kw):
            super().__init__(args, *a, **kw)

    rig = be.Rig(
        tmp=WORK, report_dir=os.path.join(WORK, "rep"),
        svc_port=be._free_port(),
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

    async def _healthy() -> bool:
        st, _ = be.op_http("GET", "/api/v1/health",
                           svc=rig.svc, token=rig.op_token)
        return st == 200

    if not await be.wait_until(_healthy, timeout_s=45, poll_s=0.5):
        print("svc dead")
        await rig.teardown()
        return 2
    rig.tab = rig.drv.new_tab("about:blank", loop)
    await rig.tab.start()
    be.TAB_Globals.rig = rig
    be.TAB_Globals.tab = rig.tab
    await rig.tab.navigate(f"{rig.svc}/fixtures/main.html", timeout=15)
    await asyncio.sleep(4)

    pre = await be.jv("""(function(){
      return JSON.stringify({
        wxDefined: typeof window.__WX__ !== 'undefined',
        wxLen: (window.__WX__||[]).length,
        bsGetter: typeof window.__BRIDGE_STATE__ ,
        st: (window.__BRIDGE_STATE__||{}).st,
        ia: localStorage.getItem('is-authenticated'),
        tok: !!localStorage.getItem('token')
      });
    })()""")
    print("PRE-LOGOUT :", pre)

    # A8.x — TRUSTED click on the fixture's real logout button
    ok = await be.click_element(rig, "#nl-logout")
    print("CLICK SENT:", ok)
    import time as _tm
    _tm.sleep(1.5)
    dfx = await be.jv("JSON.stringify(window.__DFX__ || null)")
    direct = await be.jv("try { window.__FIX__.spaLogout(); 'called-ok' } catch(e) { 'call-exc:' + String(e) }")
    print("DIRECT spaLogout() call :", direct)
    await asyncio.sleep(2.2)
    ia_d = await be.jv("localStorage.getItem('is-authenticated') + '|' + (!!localStorage.getItem('token')).toString()")
    print("storage after DIRECT    :", ia_d)
    syn = await be.jv("document.getElementById('nl-logout').dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})); 'synthetic-sent'")
    await asyncio.sleep(1.2)
    dfx0 = await be.jv("JSON.stringify(window.__DFX__ || null)")
    print("DFX after SYNTHETIC dispatch:", dfx0)
    ia0 = await be.jv("localStorage.getItem('is-authenticated') + '|' + (!!localStorage.getItem('token')).toString()")
    print("storage after synthetic  :", ia0)
    diag = await be.jv("""(function(){
      var out = {};
      out.fixKeys = window.__FIX__ ? Object.keys(window.__FIX__).join(",") : "MISSING";
      var b = document.getElementById("nl-logout");
      out.btn = b ? { w: b.offsetWidth, h: b.offsetHeight, vis: b.offsetParent !== null } : "NONE";
      if (b) {
        var r = b.getBoundingClientRect();
        out.rect = [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)];
        out.viewport = [window.innerWidth, window.innerHeight];
        out.scroll = [window.scrollX, window.scrollY];
      }
      return JSON.stringify(out);
    })()""")
    print("BTN DIAG:", diag)
    await asyncio.sleep(1.5)
    post = await be.jv("""(function(){
      var r = {
        ia: localStorage.getItem('is-authenticated'),
        tok: !!localStorage.getItem('token'),
        ntok: !!localStorage.getItem('navidrome_token'),
        st: (window.__BRIDGE_STATE__||{}).st,
        pk: (window.__BRIDGE_STATE__||{}).pk,
        hasS: null
      };
      try { r.hasS = window.__HS_PROBE__; } catch(e) {}
      return JSON.stringify(r);
    })()""")
    print("POST-LOGOUT:", post)
    ring = await be.jv("JSON.stringify((window.__WX__||[]).slice(-20))")
    print("RING TAIL  :", ring)
    await rig.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
