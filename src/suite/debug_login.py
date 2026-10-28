#!/usr/bin/env python3
"""Diagnostico interativo do login do Navidrome (chrome real + CDP).

Uso: python3 suite/debug_login.py [cred-file]
Imprime: DOM do form, resultado do fill, timeline (href, cookie NAMES,
status /rest/getInfo) por ~15s apos o submit. Nao imprime valores.
"""
import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402

SUITEPARENT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def freeport() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


JS_DOM = r"""
(function(){
  var out={href:location.href,title:document.title,
           forms:[],inputs:[],buttons:[]};
  document.querySelectorAll("form").forEach(function(f,i){
    out.forms.push({i:i,action:f.action,id:f.id,
      cls:String(f.className).slice(0,80)});
  });
  document.querySelectorAll("input,button").forEach(function(e){
    var r=e.getBoundingClientRect();
    out[(e.tagName==="BUTTON")?"buttons":"inputs"].push({
      tag:e.tagName,type:e.type,id:e.id,name:e.name,
      ph:e.placeholder,auto:e.autocomplete,vis:(r.width>0&&r.height>0)});
  });
  return out;
})()
"""

JS_FILL_USER = r"""
(function(){
  var els=Array.prototype.filter.call(document.querySelectorAll("input"),
    function(e){var r=e.getBoundingClientRect();
      return r.width>0&&r.height>0&&(e.type==="text"||e.type==="email"||e.type==="tel"||(e.autocomplete&&e.autocomplete.indexOf("username")>=0));});
  if(!els.length)
    return {ok:false,why:"sem-campo-usuario",
      all:Array.from(document.querySelectorAll("input"))
        .map(function(e){return e.type+"|"+e.autocomplete;})};
  var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,"value").set;
  s.call(els[0], USR);
  els[0].dispatchEvent(new Event("input",{bubbles:true}));
  els[0].dispatchEvent(new Event("change",{bubbles:true}));
  els[0].focus();
  return {ok:true,n:els.length,valVisible:els[0].value.length};
})()
""".replace("s.call(els[0], USR);", "s.call(els[0], __USR__);")

JS_FILL_PW = r"""
(function(){
  var el=null;
  Array.prototype.forEach.call(document.querySelectorAll("input[type=password]"),
    function(e){var r=e.getBoundingClientRect();if(r.width>0&&!el)el=e;});
  if(!el)return {ok:false,why:"sem-campo-senha"};
  var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,"value").set;
  s.call(el, __PWP__);
  el.dispatchEvent(new Event("input",{bubbles:true}));
  el.dispatchEvent(new Event("change",{bubbles:true}));
  return {ok:true,len:el.value.length};
})()
"""

JS_SUBMIT = r"""
(function(){
  var b=null;
  Array.prototype.forEach.call(
    document.querySelectorAll('button[type=submit],input[type=submit]'),
    function(e){var r=e.getBoundingClientRect();if(r.width>0&&!b)b=e;});
  if(b){b.click();return {via:"botao"};}
  var pw=document.querySelector("input[type=password]");
  if(pw){
    ["keydown","keypress","keyup"].forEach(function(t){
      pw.dispatchEvent(new KeyboardEvent(t,{key:"Enter",code:"Enter",
        keyCode:13,which:13,bubbles:true}));});
    return {via:"enter-senha"};}
  return {via:"nenhum"};
})()
"""

JS_SNAP = r"""
(function(){
  return {href:location.href,
    bodyHint:document.body.innerText.slice(0,200)};
})()
"""


async def run(cf: str) -> None:
    u, p = open(cf, "r", encoding="utf-8").readline().strip().split(":", 1)
    salt = base64.b64encode(p.encode()).decode()[:12]
    port = freeport()
    tmpd = "/tmp/dbglogin-%d" % os.getpid()
    os.makedirs(tmpd, exist_ok=True)
    sock = os.path.join(tmpd, "ipc")
    sink = subprocess.Popen(
        [sys.executable,
         os.path.join(SUITEPARENT, "tools", "fake_mpv_sink.py"),
         "--socket", sock],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = dict(os.environ)
    env.update(
        NVRA_LISTEN_ADDR="127.0.0.1", NVRA_LISTEN_PORT=str(port),
        NVRA_UPSTREAM="192.168.0.200:4533", NVRA_MPV_SOCKET=sock,
        NVRA_SPY_FILE=os.path.join(tmpd, "spy.ndjson"),
        NVRA_NAV_USER=u, NVRA_NAV_PASS=p)
    srvlog = open(os.path.join(tmpd, "srv.log"), "wb")
    srv = subprocess.Popen([sys.executable, "-m", "navidrome_remote"],
                           cwd=SUITEPARENT, env=env,
                           stdout=srvlog, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/v1/health" % port, timeout=2)
                break
            except Exception:
                if srv.poll() is not None:
                    print(open(os.path.join(tmpd, "srv.log"),
                               encoding="utf-8", errors="replace")
                          .read()[-2000:])
                    return
                time.sleep(0.3)
        origin = "http://127.0.0.1:%d" % port
        drv = cdp.BrowserDriver("/usr/bin/google-chrome")
        await asyncio.to_thread(drv.launch, 0)
        tab = drv.new_tab(origin + "/app/", asyncio.get_running_loop())
        await tab.start()
        await asyncio.sleep(2.5)

        def wrap(body: str) -> str:
            return ("(function(){ " + body.strip() + " })()")

        js_u = wrap(
            """var els=Array.prototype.filter.call(
             document.querySelectorAll("input"),function(e){
             var r=e.getBoundingClientRect();
             return r.width>0&&r.height>0&&(e.type==="text"||
             e.type==="email"||e.type==="tel"||
             (e.autocomplete&&
             e.autocomplete.indexOf("username")>=0));});
            if(!els.length)return {ok:false,why:"sem-campo-usuario",
             all:Array.from(document.querySelectorAll("input"))
             .map(function(e){return e.type+"|"+e.autocomplete;})};
            var s=Object.getOwnPropertyDescriptor(
             window.HTMLInputElement.prototype,"value").set;
            s.call(els[0], USRVX);
            els[0].dispatchEvent(new Event("input",{bubbles:true}));
            els[0].dispatchEvent(new Event("change",{bubbles:true}));
            els[0].focus();
            return {ok:true,n:els.length,len:els[0].value.length};
            """.replace("USRVX", json.dumps(u)))
        js_pw = wrap(
            """var el=null;
            Array.prototype.forEach.call(
             document.querySelectorAll("input[type=password]"),
             function(e){var r=e.getBoundingClientRect();
             if(r.width>0&&!el)el=e;});
            if(!el)return {ok:false,why:"sem-campo-senha"};
            var s=Object.getOwnPropertyDescriptor(
             window.HTMLInputElement.prototype,"value").set;
            s.call(el, PWPX);
            el.dispatchEvent(new Event("input",{bubbles:true}));
            el.dispatchEvent(new Event("change",{bubbles:true}));
            return {ok:true,len:el.value.length};
            """.replace("PWPX", json.dumps(p)))

        print("== DOM", flush=True)
        dom = await tab.eval(JS_DOM)
        print(json.dumps(dom, ensure_ascii=False, indent=1)[:3000],
              flush=True)
        print("== fill user", flush=True)
        r1 = await tab.eval(js_u)
        print(r1, flush=True)
        print("== fill pw", flush=True)
        r2 = await tab.eval(js_pw)
        print(r2, flush=True)
        print("== submit", flush=True)
        rs = await tab.eval(JS_SUBMIT)
        print(rs, flush=True)
        t0 = time.time()
        while time.time() - t0 < 18:
            await asyncio.sleep(1.5)
            snap = await tab.eval(JS_SNAP)
            try:
                cks = await tab.session.send("Storage.getCookies")
                names = sorted({c.get("name", "?")
                                for c in cks.get("cookies", [])})
            except Exception as exc:
                names = ["ERR:%s" % type(exc).__name__]
            chk = await tab.session.send(
                "Runtime.evaluate",
                {"expression":
                 "(async()=>{try{const r=await fetch("
                 "'/rest/getInfo?u=%s&s=%s&v=1.12.1&c=dbg',"
                 "{credentials:'include'});"
                 "return r.status}catch(e){return -1}})()" %
                 (u, salt),
                 "returnByValue": True, "awaitPromise": True})
            st = (((chk.get("result") or {}).get("result") or {})
                 .get("value"))
            print("t=%.0fs href=%s cookies=%s getInfo=%s"
                  % (time.time() - t0, snap.get("href"), names, st),
                  flush=True)
        href_now = (await tab.eval(JS_SNAP)) or {}
        if "/login" not in str(href_now.get("href", "")):
            print("== deep-dive (logado; caracterizando o esquema)",
                  flush=True)
            print("LS_KEYS:",
                  await tab.eval("Object.keys(localStorage)"),
                  flush=True)
            print("SS_KEYS:",
                  await tab.eval("Object.keys(sessionStorage)"),
                  flush=True)
            seen = 0
            for _ in range(40):
                try:
                    msg = await asyncio.wait_for(
                        tab.session.queue.get(), 2.0)
                except asyncio.TimeoutError:
                    break
                if (msg or {}).get("method") != "Network.requestWillBeSent":
                    continue
                req = ((msg.get("params") or {}).get("request")) or {}
                url = req.get("url", "")
                hd = req.get("headers") or {}
                out = {}
                for k, v in hd.items():
                    kl = k.lower()
                    if kl == "cookie":
                        masked = ",".join(
                            cv.split("=", 1)[0] + "=***"
                            for cv in str(v).split(";")
                            if "=" in cv)
                        out[k] = masked
                    elif kl == "authorization":
                        out[k] = "***masked(len=%d)" % len(str(v))
                    elif kl in ("referer", "origin"):
                        out[k] = str(v)[:60]
                print("REQ:", req.get("method"), cdp.redact(url)[:120],
                      out, flush=True)
                seen += 1
                if seen >= 8:
                    break
        print("== done", flush=True)
        await tab.aclose()
        drv.shutdown()
    finally:
        srv.terminate()
        try:
            srv.wait(5)
        except Exception:
            srv.kill()
        sink.terminate()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("uso: debug_login.py <cred-file-0600> "
                 "(/tmp/nr-a2-creds aposentada em A0.1.3)")
    cf = sys.argv[1]
    asyncio.run(run(cf))
