#!/usr/bin/env python3
"""player_boot_capture.py — A2: captura do boot do player Navidrome (Chrome
headless REAL) contra o serviço único (origin publicado = listener do proxy).

Aceite A2:
  1) rotas chamadas no boot (tabela: rota × tipo × contagem);
  2) localização do transporte (onde o áudio anda: HTML5 audio sobre
     /rest/stream? SSE? WS?) — veredito por evidência;
  3) emite WS/SSE? (contadores CDP: webSocketCreated/Opened + respostas
     text/event-stream);
  4) Cláusula B(ii): sessão/Set-Cookie sobrevivendo a reload e navegação
     no origin publicado (navegador real, conta de serviço).

Saídas (diretório --out-dir):
  a2-<stamp>.md            relatório legível
  a2-<stamp>.json          resumo máquina (input do gate)
  a2-<stamp>-trace.ndjson  traço CDP (sanitizado)
  a2-<stamp>-spy.ndjson    cópia do espiao do serviço (se sobe aqui)

Segurança: a credencial só entra por --cred-file (0600, ponteiro do
operador). Jamais sai para relatório/rastro (mascaras). No máx. 2
tentativas de login (o /auth/login carrega X-RateLimit: 5).
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
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)                      # .../src
REPO = os.path.dirname(SRC)                     # raiz do repo
sys.path.insert(0, HERE)
from cdp import BrowserDriver, redact  # noqa: E402

# A0.1.3 (2026-10-28): convencao /tmp/nr-a2-creds APOSENTADA.
# Credencial chega via --cred-file (caminho privado 0600, staging proprio).
DEFAULT_CRED_FILE = ""
# Middleware v0.64.2: sondagem com APENAS s= devolve code=40 MESMO com
# credencial valida (achado A0.1.3, registrado na suite). Vetores aceitos:
# p=<senha> | t=MD5(pass+salt)+s=<salt> | jwt=<token>. Esta suite usa p=.
AUTH_CHECK_Q_TMPL = "/rest/getInfo?p={pwd}&v=1.12.1&c=a2-bootcap&api=1.12.1"


# ------------------------------------------------------------------- setup
def read_cred(path: str) -> "tuple[str, str]":
    with open(path, "r", encoding="utf-8") as fh:
        line = fh.readline().strip()
    if ":" not in line:
        raise SystemExit(f"cred-file mal formado (esperado user:pass): {path}")
    u, p = line.split(":", 1)
    return u.strip(), p


def subsonic_salt(pw: str) -> str:
    return base64.b64encode(pw.encode()).decode()[:12]


def http_get(url: str, timeout: float = 6.0) -> "tuple[int, bytes]":
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        return -1, str(exc).encode()


def http_post_json(url: str, obj, timeout: float = 6.0) -> "tuple[int, str]":
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, str(exc)


class Bringup:
    """Sobe mock-sink + serviço único (ou anexa a um já no ar)."""

    def __init__(self, args, user: str, pw: str):
        self.args = args
        self.user = user
        self.pw = pw
        self.tmprd = tempfile.mkdtemp(prefix="nvra2-")
        self.sock = os.path.join(self.tmprd, "svc-ipc.sock")
        self.spy = os.path.join(self.tmprd, "spy.ndjson")
        self.log = os.path.join(self.tmprd, "srv.log")
        self.procs: list = []

    def up(self) -> None:
        if self.args.attach:
            return
        env = dict(os.environ)
        env.update({
            "NVRA_LISTEN_ADDR": self.args.listen_addr,
            "NVRA_LISTEN_PORT": str(self.args.listen_port),
            "NVRA_UPSTREAM": self.args.upstream,
            "NVRA_UPSTREAM_VHOST": self.args.vhost,
            "NVRA_MPV_SOCKET": self.sock,
            "NVRA_SPY_FILE": self.spy,
            "NVRA_NAV_USER": self.user,
            "NVRA_NAV_PASS": self.pw,
        })
        self.procs.append(subprocess.Popen(
            [sys.executable, os.path.join(SRC, "tools", "fake_mpv_sink.py"),
             "--socket", self.sock],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.procs.append(subprocess.Popen(
            [sys.executable, "-m", "navidrome_remote"],
            stdout=open(self.log, "wb"), stderr=subprocess.STDOUT,
            cwd=SRC, env=env))

    def wait_health(self, seconds: float = 20.0) -> None:
        url = f"http://127.0.0.1:{self.args.listen_port}/api/v1/health"
        deadline = time.time() + seconds
        while time.time() < deadline:
            st, _b = http_get(url, timeout=3)
            if st == 200:
                return
            time.sleep(0.3)
        tail = ""
        if not self.args.attach:
            try:
                tail = open(self.log, "rb").read()[-1500:].decode(
                    "utf-8", "replace")
            except OSError:
                pass
        raise SystemExit(
            f"FATAL: serviço não healthy em {seconds}s ({url})\n{tail}")

    def down(self) -> None:
        for p in self.procs:
            try:
                p.terminate()
            except OSError:
                pass
        for p in self.procs:
            try:
                p.wait(3)
            except Exception:
                try:
                    p.kill()
                except OSError:
                    pass
        shutil.rmtree(self.tmprd, ignore_errors=True)

    def spy_copy(self, dst: str) -> None:
        try:
            with open(self.spy, "rb") as fh:
                data = fh.read()
            with open(dst, "wb") as fh:
                fh.write(data)
        except OSError:
            pass


# ------------------------------------------------------------------ js bits
FORM_PROBE_JS = (
    "(function(){var f=document.querySelectorAll('input[type=password]');"
    "return f.length?"
    "{pw:f.length,text:document.querySelectorAll('input[type=text]').length}:"
    "null;})()"
)

JS_USERNAME_TMPL = (
    "(function(){"
    "var cands=Array.prototype.filter.call("
    "document.querySelectorAll('input[type=text],input[autocomplete=username],"
    "input:not([type])'),"
    "function(e){return !e.disabled && e.offsetParent !== null;});"
    "if(!cands.length)return {ok:false,msg:'sem-campo-usuario'};"
    "var el=cands[0];"
    "var setter=Object.getOwnPropertyDescriptor("
    "window.HTMLInputElement.prototype,'value').set;"
    "setter.call(el,__USR__);el.dispatchEvent(new Event('input'"
    ",{bubbles:true}));"
    "return {ok:true};"
    "})()"
)

JS_PASSWORD_TMPL = (
    "(function(){"
    "var el=document.querySelector('input[type=password]');"
    "if(!el)return {ok:false,msg:'sem-campo-senha'};"
    "var setter=Object.getOwnPropertyDescriptor("
    "window.HTMLInputElement.prototype,'value').set;"
    "setter.call(el,__PWP__);el.dispatchEvent(new Event('input'"
    ",{bubbles:true}));"
    "return {ok:true};"
    "})()"
)

JS_SUBMIT = (
    "(function(){"
    "var b=document.querySelector('button[type=submit]');"
    "if(b){b.click();return {ok:true,via:'submit'};}"
    "var pw=document.querySelector('input[type=password]');"
    "if(pw){pw.dispatchEvent(new KeyboardEvent('keydown',"
    "{key:'Enter',code:'Enter',keyCode:13,bubbles:true}));"
    "return {ok:true,via:'enter'};}"
    "return {ok:false,msg:'sem-botao-submit'};"
    "})()"
)

PLAY_BUTTON_TMPL = (
    "(function(){"
    "var sels=[__SEL__];"
    "for(var i=0;i<sels.length;i++){"
    "var list=Array.prototype.filter.call("
    "document.querySelectorAll(sels[i]),"
    "function(e){return e.offsetParent!==null;});"
    "for(var j=0;j<Math.min(list.length,3);j++){"
    "var el=list[j];"
    "try{el.click();return {ok:true,sel:sels[i],idx:j};"
    "}catch(_e){}}"
    "}"
    "return {ok:false,why:'nenhum-botao-de-play-visivel'};"
    "})()"
)

PLAY_SELECTOR_IDS = [
    'button[aria-label*="play" i]',
    '[data-testid*="play"]',
    'button[class*="Play"]',
    '[class*="play-button"]',
]

STOP_JS = (
    "(function(){var sel='"
    "button[aria-label*=\"pause\" i],[aria-label*=\"stop\" i],button[data-stop]'"
    ";var b=document.querySelector(sel);"
    "if(b){b.click();return true;}return false;})()"
)

AUDIO_ELEM_JS = (
    "(function(){return Array.from(document.querySelectorAll('audio')).map("
    "function(a){return {src:a.src||a.getAttribute('src')||''"
    ",currentSrc:a.currentSrc||'',paused:a.paused,time:a.currentTime,"
    "duration:a.duration,readyState:a.readyState,volume:a.volume,"
    "muted:a.muted};});})()"
)

BRIDGE_LOADED_JS = "!!window.__NVRA_BRIDE" .replace(
    "NVRA_BRIDE", "NVRA_BRIDGE__")

VERSION_IN_PAGE_JS = (
    "(String(window.__APP_CONFIG__||'').match(/\"version\":\"([^\"]+)\"/)"
    "||[])[1]||null"
)


LS_AUTH_JS = (
    "(function(){"
    "try{"
    "var t=localStorage.getItem('token');"
    "return {"
    "authed:(localStorage.getItem('is-authenticated')==='true'),"
    "hasToken:!!t, tokLen:t?t.length:0,"
    "hasU:!!localStorage.getItem('subsonic-token'),"
    "hasS:!!localStorage.getItem('subsonic-salt')};"
    "}catch(e){return {err:String(e)};}"
    "})()"
)


def ls_auth_ok(v) -> bool:
    return bool(v and v.get("authed") and v.get("hasToken"))


def js_auth_check(pwd: str) -> str:
    """Probe RESIDUAL via vetor p=<password>; composto com o check do LS."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pwd):
        raise ValueError("cred-pass com caracteres nao-URL-safe; abortando")
    q = AUTH_CHECK_Q_TMPL.format(pwd=pwd)
    return (
        "(async function(){try{var r=await fetch('" + q + "',"
        "{credentials:'include'});return {status:r.status};"
        "}catch(e){return {status:-1,error:String(e)};}})()"
    )


async def tab_eval_async(tab, expr: str, timeout: float = 25.0):
    """Runtime.evaluate com awaitPromise (para fetch em-página)."""
    resp = await tab.session.send(
        "Runtime.evaluate",
        {"expression": expr, "returnByValue": True, "awaitPromise": True},
        timeout=timeout)
    if "exceptionDetails" in resp:
        det = resp["exceptionDetails"]
        exc = det.get("exception") or {}
        desc = exc.get("description", "") if isinstance(exc, dict) else ""
        raise RuntimeError(
            "eval falhou: %s %s" % (redact(str(det.get("text", ""))),
                                    redact(desc)))
    res = resp.get("result") or {}
    return res.get("value")


def js_play_button() -> str:
    return PLAY_BUTTON_TMPL.replace(
        "__SEL__", ",".join(json.dumps(s) for s in PLAY_SELECTOR_IDS))


# ------------------------------------------------------------------ analysis
def route_bucket(url: str) -> str:
    """Agrega URLs repetidas em buckets legíveis (query ignorada)."""
    p = urlparse(url)
    path = p.path or "/"
    if path.startswith("/rest/"):
        parts = path.split("/")
        return "/rest/%s" % parts[2] if len(parts) > 2 and parts[2] else "/rest/*"
    if path.startswith("/app/assets/") or "/assets/" in path:
        return "/assets/*"
    if path in ("/",):
        return "/"
    if path.startswith("/app/"):
        segs = path.rstrip("/").split("/")
        if len(segs) >= 3:
            return "/app/" + segs[2]
        return "/app"
    head = path.split("/")[1] if "/" in path[1:] else path
    if head and len(head) <= 24 and not head.isdigit():
        return "/" + head + "*"
    return path


def analyze_requests(reqs: list) -> dict:
    buckets: dict = {}
    for r in reqs:
        b = route_bucket(r.get("url") or "?")
        rec = buckets.setdefault(b, {
            "count": 0, "types": set(), "statuses": set(),
            "sample": r.get("url", ""), "setCookies": set(),
        })
        rec["count"] += 1
        rec["types"].add(r.get("resourceType") or "?")
        resp = r.get("resp") or {}
        st = resp.get("status")
        if st is not None and st >= 0:
            rec["statuses"].add(st)
        for n in resp.get("setCookieNames") or []:
            rec["setCookies"].add(n)
    out = {}
    for k in sorted(buckets, key=lambda kk: -buckets[kk]["count"]):
        v = buckets[k]
        out[k] = {
            "count": v["count"],
            "types": sorted(v["types"]),
            "statuses": sorted(v["statuses"]),
            "sample": v["sample"],
            "setCookies": sorted(v["setCookies"]),
        }
    return out


def detect_transport(reqs: list, audio_elems, tab) -> dict:
    stream_hits = []
    sse_paths = []
    for r in reqs:
        u = r.get("url") or ""
        base = u.split("?")[0]
        if base.endswith("/rest/stream") or "/rest/stream?" in u:
            stream_hits.append({
                "url": redact(u),
                "range": (r.get("reqHeadersKept") or {}).get("range"),
                "ctype": (r.get("resp") or {}).get("mime"),
                "status": (r.get("resp") or {}).get("status"),
            })
        mime = ((r.get("resp") or {}).get("mime") or "").lower()
        if "event-stream" in mime:
            sse_paths.append(redact(u))
    parts = []
    if tab.ws_opened:
        parts.append(
            "WEBSOCKET (created=%d, opened=%d, urls=%s)"
            % (tab.ws_created, tab.ws_opened, tab.ws_urls[:3]))
    if sse_paths:
        parts.append("SSE em %s" % sorted(set(sse_paths))[:4])
    if stream_hits:
        ranges = {h.get("range") for h in stream_hits if h.get("range")}
        parts.append(
            "HTTP streaming /rest/stream (%d reqs%s)"
            % (len(stream_hits),
               ", range(s)=%s" % sorted(ranges) if ranges else ""))
    if not parts:
        parts.append("INCONCLUSIVO neste trecho (tocar a faixa?)")
    return {
        "verdict": " + ".join(parts),
        "streamHits": stream_hits[:12],
        "ssePaths": sorted(set(sse_paths))[:8],
        "ws": {
            "created": tab.ws_created, "opened": tab.ws_opened,
            "frames_in": tab.ws_frames_in, "frames_out": tab.ws_frames_out,
            "urls": tab.ws_urls[:5],
        },
        "audioElements": [
            {k: (redact(v) if isinstance(v, str) else v)
             for k, v in (a or {}).items()}
            for a in (audio_elems or [])
        ],
    }


def _advancing(before, after) -> bool:
    if not before or not after:
        return False
    a, b = before[0], after[0]
    ta = a.get("time") or 0.0
    tb = b.get("time") or 0.0
    return (tb > ta) or (bool(a.get("paused")) and not bool(b.get("paused")))


def extract_song_ids(xml_body: str) -> list:
    return [
        m.group(1)
        for m in re.finditer(r'id="([^"]+)"', xml_body[:2000])
    ][0:8] or []


# -------------------------------------------------------------------- flow
async def capture_flow(tab, cfg, salt: str, origin: str,
                       summary: dict) -> int:
    # G0 — origem publicada: versão lida na própria página (âncora).
    await tab.navigate(origin + "/")
    await asyncio.sleep(2.0)
    v_in = await tab.eval(VERSION_IN_PAGE_JS)
    st, body = http_get(origin + "/rest/ping?x=1", timeout=5)
    m = re.search(rb'serverVersion="([^"]+)"', body)
    summary["anchor"] = {
        "in_browser": v_in,
        "server_ping": m.group(1).decode() if m else None,
    }

    # G1 — login (formulário primeiro; fallback /auth/login).
    form = await tab.eval(FORM_PROBE_JS)
    already = False
    if form is None:
        # v0.64.2: sessao persiste no localStorage; evita re-login.
        try:
            ls = await tab.eval(LS_AUTH_JS)
            already = ls_auth_ok(ls)
        except Exception:
            ls = None
        if not already:
            chk = await tab_eval_async(
                tab, js_auth_check(cfg.cred_pass))
            already = bool(chk and chk.get("status") == 200)
    summary["login_initial_state"] = {
        "formPresent": bool(form and form.get("pw")),
        "alreadyLoggedIn": already,
    }
    if not already:
        lc = await do_login(tab, cfg, salt, origin, summary)
        if lc != 0:
            summary["fatal"] = "login"
            return 1
    else:
        summary["login"] = {"ok": True, "via": "ja-autenticado"}

    # G2 — janela de boot.
    tab.mark()
    await asyncio.sleep(cfg.boot_window)
    boot_reqs = tab.requests_since_mark()
    audio1 = await tab_eval_async(tab, AUDIO_ELEM_JS)
    summary["boot"] = {
        "window_s": cfg.boot_window,
        "request_count": len(boot_reqs),
        "routes": analyze_requests(boot_reqs),
    }
    summary["transport_idle"] = detect_transport(boot_reqs, audio1, tab)

    # G3 — play (UI primeiramente; API como fallback/rede).
    play: dict = {"mode": cfg.play, "uiClicked": False, "apiUsed": False}
    if cfg.play in ("ui", "both"):
        res = await tab.eval(js_play_button())
        if res and res.get("ok"):
            play["uiClicked"] = True
            play["via"] = res.get("sel")
            tab.mark()
            await asyncio.sleep(7.0)
            audio_a = await tab_eval_async(tab, AUDIO_ELEM_JS)
            await asyncio.sleep(2.5)
            audio_b = await tab_eval_async(tab, AUDIO_ELEM_JS)
            play["progress"] = _advancing(audio_a, audio_b)
            play["audioAfter"] = (
                [{k: (redact(v) if isinstance(v, str) else v)
                  for k, v in (audio_b or [{}])[0].items()},])
            post_reqs = tab.requests_since_mark()
            summary["transport_play"] = detect_transport(
                post_reqs + boot_reqs, audio_b or audio_a, tab)
        else:
            play["uiClickDetail"] = res
        summary["play"] = play
    if (cfg.play in ("api", "both")
            or (cfg.play == "ui" and not play.get("uiClicked"))):
        await try_api_play(tab, cfg, salt, summary, play)
        summary["play"] = play

    # G4 — bridge.js executou no navegador real?
    bridged = await tab.eval(BRIDGE_LOADED_JS)
    summary["bridge"] = {"loaded_in_browser": bool(bridged)}

    # G5 — B(ii): sobrevivência de sessão no origin publicado.
    if not cfg.skip_bii:
        await bii_phase(tab, cfg, salt, origin, summary)
    else:
        summary["bii"] = {"skipped": True}

    # parada cortês (melhor esforço)
    await tab.eval(STOP_JS)
    await asyncio.sleep(0.5)
    return 0


async def do_login(tab, cfg, salt: str, origin: str,
                   summary: dict) -> int:
    form = await tab.eval(FORM_PROBE_JS)
    tried_form = False
    if not (form and form.get("pw")):
        await tab.navigate(origin + "/login")
        await asyncio.sleep(2.0)
        form = await tab.eval(FORM_PROBE_JS)
    if form and form.get("pw"):
        tried_form = True
        r1 = await tab.eval(JS_USERNAME_TMPL.replace(
            "__USR__", json.dumps(cfg.cred_user)))
        r2 = await tab.eval(JS_PASSWORD_TMPL.replace(
            "__PWP__", json.dumps(cfg.cred_pass)))
        if not (r1 and r1.get("ok") and r2 and r2.get("ok")):
            summary["login"] = {
                "ok": False, "via": "form-fill",
                "detail": "fill falhou: %s %s" % (r1, r2),
            }
            return 1
        await tab.eval(JS_SUBMIT)
        last_chk = None
        ls = None
        for _ in range(12):
            await asyncio.sleep(1.0)
            try:
                last_chk = await tab_eval_async(
                    tab, js_auth_check(cfg.cred_pass))
            except Exception:
                continue
            try:
                ls = await tab.eval(LS_AUTH_JS)
            except Exception:
                ls = None
            gone = (await tab.eval(FORM_PROBE_JS)) is None
            if ls_auth_ok(ls) or (gone
                                   and last_chk
                                   and last_chk.get("status") == 200):
                summary["login"] = {
                    "ok": True, "via": "formulario",
                    "evidence": ls,
                }
                return 0
        summary["login"] = {
            "ok": False, "via": "form",
            "detail": ("form submetido, sessao nao efetivada "
                       "(ls=%s getInfo=%s)" % (ls, last_chk)),
        }
        return 1
    # fallback: POST direto em-página (1 tentativa; X-RateLimit: 5).
    payload = json.dumps(json.dumps(
        {"username": cfg.cred_user, "password": cfg.cred_pass}))
    expr = (
        "(async function(){var r=await fetch('/auth/login',"
        "{method:'POST',headers:{'content-type':'application/json'},"
        "body:JSON.parse(%s),credentials:'include'});"
        "var t=await r.text();"
        "var keys=[];try{keys=Object.keys(JSON.parse(t))}catch(e){}"
        "return {status:r.status,keys:keys};})()" % payload)
    try:
        r3 = await tab_eval_async(tab, expr)
    except Exception as exc:
        summary["login"] = {
            "ok": False, "via": "auth-post",
            "detail": redact(str(exc)),
        }
        return 1
    if not r3 or r3.get("status") not in (200, 201):
        summary["login"] = {
            "ok": False, "via": "auth-post",
            "detail": "status=%s" % (r3,),
        }
        return 1
    summary["login"] = {
        "ok": True, "via": "auth-login-post",
        "note": "bypass raw: chaves da resposta=%s; o LS do SPA "
                "nao e restaurado nesse caminho (verificacao LS "
                "primaria segue via form)" % (r3.get("keys"),),
    }
    return 0


GETTRACKS_JS_TMPL = (
    "(async function(){try{"
    "var u=localStorage.getItem('subsonic-token')||'%u';"
    "var s=localStorage.getItem('subsonic-salt')||%s;"
    "var tk=localStorage.getItem('token');"
    "var hdrs=tk?{authorization:'*** '+tk}:{};"
    "var eps=['/rest/getRandomTracks','/rest/getRandomSongs'];"
    "for(var i=0;i<eps.length;i++){"
    "var r=await fetch(eps[i]+"
    "?u='+encodeURIComponent(u)+"
    "&s='+encodeURIComponent(s)+'&v=1.12.1&c=a2-bootcap"
    "&size=1&libraryId=1',{headers:hdrs,credentials:'include'});"
    "if(r.status===200){"
    "var t=await r.text();"
    "return {status:r.status,ep:eps[i],"
    "body:t.slice(0,600)};}}"
    "return {status:r.status};}"
    "catch(e){return {status:-1,err:String(e)};}})()"
)


async def try_api_play(tab, cfg, salt: str, summary: dict,
                       play: dict) -> None:
    expr = GETTRACKS_JS_TMPL % (
        cfg.cred_user, json.dumps(subsonic_salt(cfg.cred_pass)))
    try:
        chk = await tab_eval_async(tab, expr)
    except Exception as exc:
        play["apiError"] = redact(str(exc))
        return
    if not chk or chk.get("status") != 200:
        play["apiError"] = "getRandom* status=%s" % (
            (chk or {}).get("status"),)
        return
    play["apiEndpoint"] = chk.get("ep")
    ids = extract_song_ids(chk.get("body") or "")
    if not ids:
        play["apiError"] = "sem id no body (%s)" % (chk.get("ep"),)
        return
    st, body = http_post_json(
        "http://127.0.0.1:%d/api/v1/play" % cfg.listen_port,
        {"id": ids[0]}, timeout=6)
    if st != 200:
        play["apiError"] = "/api/v1/play http %s: %s" % (st, redact(body))
        return
    play["apiUsed"] = True
    play["songId"] = ids[0]
    play["apiAccepted"] = redact(body)


async def bii_phase(tab, cfg, salt: str, origin: str,
                    summary: dict) -> None:
    """Cláusula B(ii): sessão (cookie e/ou LS) sobe a reload e navegação."""

    async def one(label: str, reload: bool, dest: str) -> dict:
        before = await tab.get_cookie_names()
        names_before = {c["name"] for c in before}
        if reload:
            await tab.reload()
        else:
            await tab.navigate(dest)
        await asyncio.sleep(1.5)
        after = await tab.get_cookie_names()
        names_after = {c["name"] for c in after}
        try:
            ls = await tab.eval(LS_AUTH_JS)
        except Exception:
            ls = None
        try:
            chk = await tab_eval_async(
                tab, js_auth_check(cfg.cred_pass))
        except Exception:
            chk = {"status": -1}
        form = await tab.eval(FORM_PROBE_JS)
        info_ok = bool(chk and chk.get("status") == 200
                       and not (form and form.get("pw")))
        return {
            "step": label,
            "cookiesBefore": sorted(names_before),
            "cookiesAfter": sorted(names_after),
            "surviving": sorted(names_after & names_before),
            "ls": ls,
            "authStatus": chk.get("status") if chk else None,
            "authOk": ls_auth_ok(ls) or info_ok,
            "loginFormBack": bool(form and form.get("pw")),
        }

    steps = []
    steps.append(await one("reload-1", True, origin + "/"))
    steps.append(await one("reload-2", True, origin + "/"))
    steps.append(await one("nav-raiz", False, origin + "/"))
    steps.append(await one("nav-app", False, origin + "/app/"))
    # v0.64.2: sessao = localStorage (JWT token + subsonic u/s) — SEM
    # cookie. Cláusula B(ii): a sessão sobrevive a reload e navegação no
    # origin publicado. Verde: auth preservada em todo passo (LS ou
    # residual) + form de login nunca volta. Cookies, quando existirem
    # (outras versões), também devem sobreviver; se o conjunto vazio,
    # nada a exigir (não fabricamos requisito sobre cookie inexistente).
    auth_held = all(s["authOk"] for s in steps)
    form_never_back = not any(s["loginFormBack"] for s in steps)
    has_cookies = any(s["cookiesBefore"] or s["cookiesAfter"]
                      for s in steps)
    cookies_ok = (not has_cookies
                  or all(not s["cookiesBefore"] or s["surviving"]
                         for s in steps))
    green = auth_held and form_never_back and cookies_ok
    summary["bii"] = {
        "green": green,
        "requirement": ("sessao do Navidrome sobrevivendo a reload/"
                        "navegacao no origin publicado"),
        "scheme": ("v0.64.2: localStorage (is-authenticated, token, "
                   "subsonic-token, subsonic-salt); cookie = "
                   "residual/outra versao"),
        "details": {
            "authHeldAcrossSteps": auth_held,
            "loginFormNeverReturned": form_never_back,
            "cookiesObserved": has_cookies,
            "cookiesOk": cookies_ok,
        },
        "steps": steps,
    }


# ------------------------------------------------------------------- writers
def write_trace(path: str, reqs: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in reqs:
            rec = {
                "ts": r.get("ts"), "method": r.get("method"),
                "url": r.get("url"), "type": r.get("resourceType"),
            }
            resp = r.get("resp")
            if resp:
                rec["status"] = resp.get("status")
                rec["mime"] = resp.get("mimeType")
                sc = resp.get("setCookieNames")
                if sc:
                    rec["setCookies"] = sc
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def count_bridge_beacons(spy_path: str) -> "tuple[int, list]":
    n = 0
    samples = []
    try:
        for line in open(spy_path, errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("event") == "bridge":
                n += 1
                if len(samples) < 5:
                    samples.append(
                        {"beacon": obj.get("beacon"),
                         "detail": redact(obj.get("detail", ""))[:160]})
    except OSError:
        pass
    return n, samples


def write_summary_json(path: str, summary: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False, default=str)


def write_report(path: str, summary: dict) -> None:
    L: list = []
    ap = L.append
    ap("# Relatório A2 — boot do player v0.64.2 (navegador real, C1)")
    ap("")
    ap("- **Quando:** %s (UTC)" % summary.get("stamp"))
    ap("- **Origin publicado (listener do proxy):** `%s`"
       % summary.get("origin"))
    up = summary.get("upstream") or ""
    ap("- **Upstream:** `%s`%s"
       % (up, (" (vhost `%s`)" % summary.get("vhost"))
          if summary.get("vhost") else ""))
    anch = summary.get("anchor") or {}
    ap("- **Âncora:** lida na página `%s` | /rest/ping black-box `%s`"
       % (anch.get("in_browser"), anch.get("server_ping")))
    lg = summary.get("login") or {}
    ap("- **Login (serviço-account `%s`):** %s (%s)"
       % (summary.get("cred_user"),
          "OK" if lg.get("ok") else "FALHOU", lg.get("via", "?")))
    if summary.get("fatal"):
        ap("- **PARCIAL/FATAL:** `%s`" % redact(summary["fatal"]))
    ap("")

    boot = summary.get("boot") or {}
    ap("## 1. Rotas chamadas (boot, %.0fs)" % (boot.get("window_s") or 0))
    ap("")
    ap("| rota (bucket) | n | tipos | status | amostra | Set-Cookie |")
    ap("|---|---|---|---|---|---|")
    for r, d in (boot.get("routes") or {}).items():
        sts = "/".join(str(s) for s in d["statuses"]) or "-"
        ap("| `%s` | %d | %s | %s | `%s` | %s |"
           % (r, d["count"], ",".join(d["types"]), sts,
              (d.get("sample") or "")[:110],
              ", ".join(d["setCookies"]) or "-"))
    ap("")

    for key, label in (("transport_idle", "repouso (boot)"),
                       ("transport_play", "após play")):
        t = summary.get(key)
        if not t:
            continue
        ap("## 2. Localização do transporte (%s)" % label)
        ap("")
        ap("**Veredito:** %s" % t.get("verdict"))
        ap("")
        ap("| marcador | valor |")
        ap("|---|---|")
        ws = t.get("ws") or {}
        ap("| WebSocket created/opened | %s/%s (frames in/out: %s/%s) |"
           % (ws.get("created", 0), ws.get("opened", 0),
              ws.get("frames_in", 0), ws.get("frames_out", 0)))
        ap("| SSE (text/event-stream) | %s |"
           % (", ".join(t.get("ssePaths") or []) or "nenhum"))
        sh = t.get("streamHits") or []
        ap("| hits `/rest/stream` | %d |" % len(sh))
        for h in sh[:6]:
            ap("   - `%s` range=%s ctype=%s status=%s"
               % ((h.get("url") or "")[:96], h.get("range"),
                  h.get("ctype"), h.get("status")))
        ae = t.get("audioElements") or []
        ap("| elementos `<audio>` | %d |" % len(ae))
        for a in ae[:4]:
            src = a.get("src") or a.get("currentSrc") or ""
            ap("   - src=`%s` paused=%s t=%s readyState=%s"
               % ((src or "")[:96], a.get("paused"),
                  a.get("time"), a.get("readyState")))
        ap("")

    wsi = (summary.get("transport_idle") or {}).get("ws") or {}
    spp = set()
    for k in ("transport_idle", "transport_play"):
        spp |= set((summary.get(k) or {}).get("ssePaths") or [])
    ap("## 3. Emite WS/SSE? (pergunta literal do aceite)")
    ap("")
    ap("- **WebSocket:** %s (created=%s)"
       % ("SIM" if (wsi.get("opened") or 0) > 0 else "NÃO observado",
          wsi.get("created", 0)))
    ap("- **SSE:** %s"
       % ("SIM: %s" % sorted(spp) if spp else "NÃO observado"))
    ap("- *(evidência: eventos CDP `Network.webSocket*` e respostas "
       "`text/event-stream`; payloads de WS nunca registrados)*")
    ap("")

    br = summary.get("bridge") or {}
    ap("## 4. bridge.js (execução no navegador real)")
    ap("")
    ap("- `window.__NVRA_BRIDGE__` definido: **%s**"
       % br.get("loaded_in_browser"))
    bc = summary.get("bridge_beacons") or {}
    ap("- beacons no spy (`event:bridge`): **%s**"
       % bc.get("count", "?"))
    for s in bc.get("samples", [])[:5]:
        ap("   - `%s` — %s" % (s.get("beacon"), s.get("detail")))
    ap("")

    bii = summary.get("bii")
    if isinstance(bii, dict) and "green" in bii:
        ap("## 5. Cláusula B(ii) — sessão no origin publicado (navegador real)")
        ap("")
        ap("**Veredito: %s** — %s."
           % ("VERDE" if bii["green"] else "VERMELHO",
              bii.get("requirement", "")))
        ap("")
        if bii.get("scheme"):
            ap("- **Esquema da sessao (esta versao):** %s" % bii["scheme"])
        det = bii.get("details") or {}
        for k_ in ("authHeldAcrossSteps", "loginFormNeverReturned",
                   "cookiesObserved", "cookiesOk"):
            if k_ in det:
                ap("- %s: %s" % (k_, det[k_]))
        ap("")
        ap("| passo | cookies após (nomes) | LS auth | getInfo | form voltou? |")
        ap("|---|---|---|---|---|")
        for s in bii.get("steps", []):
            ls_ = s.get("ls") or {}
            if ls_.get("authed") in (True, False):
                lsp = ("authed=%s tok=%s u=%s s=%s"
                       % (ls_.get("authed"), ls_.get("hasToken"),
                          ls_.get("hasU"), ls_.get("hasS")))
            elif ls_.get("err"):
                lsp = "err"
            else:
                lsp = "-"
            ap("| %s | %s | %s | %s | %s |"
               % (s["step"], ", ".join(s["cookiesAfter"]) or "-",
                  lsp, s["authStatus"], s["loginFormBack"]))
        ap("")

    p = summary.get("play")
    if p:
        ap("## 6. Exercício de play")
        ap("")
        ap("- modo: `%s` | clique UI: %s%s | API: %s%s"
           % (p.get("mode"), p.get("uiClicked"),
              (" via %s" % p["via"]) if p.get("via") else "",
              p.get("apiUsed"),
              (" songId=%s" % p.get("songId")) if p.get("songId") else ""))
        if p.get("progress") is not None:
            ap("- progresso (clock do áudio): %s" % p.get("progress"))
        if p.get("apiError"):
            ap("- detalhe API: `%s`" % redact(p["apiError"]))
        ap("")

    ap("---")
    ap("_Gerado por `suite/player_boot_capture.py` (A2). Rastro sanitizado: "
       "valores de cookie/auth e sal Subsonic mascarados. Credencial da "
       "conta de serviço: somente ponteiro, nunca valor._")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="A2: boot do player v0.64.2 via navegador real (C1)")
    ap.add_argument("--listen-addr", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=8481)
    ap.add_argument("--upstream", default="192.168.0.200:4533")
    ap.add_argument("--vhost", default="")
    ap.add_argument("--cred-file", default=DEFAULT_CRED_FILE)
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--out-dir", default=os.path.join(SRC, "docs", "reports"))
    ap.add_argument("--attach", action="store_true",
                    help="anexa a um serviço já no ar (não sobe mock/uv)")
    ap.add_argument("--boot-window", type=float, default=20.0,
                    help="segundos de captura de boot")
    ap.add_argument("--play", choices=("ui", "api", "both", "none"),
                    default="ui")
    ap.add_argument("--skip-bii", action="store_true",
                    help="pular etapa B(ii)")
    args = ap.parse_args()

    if not args.cred_file or not os.path.exists(args.cred_file):
        print("[BOOT-CAP] --cred-file ausente/invalido: B(ii) requer "
              "credencial (convencao /tmp/nr-a2-creds APOSENTADA em "
              "A0.1.3); modo anonimo = --skip-browser.",
              file=sys.stderr)
        sys.exit(2)
    user, pw = read_cred(args.cred_file)
    salt = subsonic_salt(pw)
    args.cred_user, args.cred_pass = user, pw

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    base = "a2-%s" % stamp
    os.makedirs(args.out_dir, exist_ok=True)

    summary: dict = {
        "stamp": stamp,
        "origin": "http://%s:%d" % (args.listen_addr, args.listen_port),
        "upstream": args.upstream,
        "vhost": args.vhost,
        "cred_user": user,
        "steps": {},
    }
    trace_rows: list = []
    bring = Bringup(args, user, pw)
    bring.up()
    bring.wait_health()

    rc = 1
    try:
        rc = asyncio.run(run_capture(args, salt, summary, trace_rows))
    finally:
        spy_dst = os.path.join(args.out_dir, base + "-spy.ndjson")
        bring.spy_copy(spy_dst)
        bring.down()
        nb, samples = count_bridge_beacons(spy_dst)
        summary["bridge_beacons"] = {"count": nb, "samples": samples}

    md_path = os.path.join(args.out_dir, base + ".md")
    json_path = os.path.join(args.out_dir, base + ".json")
    trace_path = os.path.join(args.out_dir, base + "-trace.ndjson")
    write_trace(trace_path, trace_rows)
    write_summary_json(json_path, summary)
    write_report(md_path, summary)
    print(json.dumps({
        "ok": rc == 0,
        "report": md_path,
        "json": json_path,
        "trace": trace_path,
        "spyCopy": spy_dst,
    }, indent=2, ensure_ascii=False))
    return rc


async def run_capture(args, salt: str, summary: dict,
                      trace_rows: list) -> int:
    from cdp import Tab  # noqa: F401 (import tardio p/ facilitar mocks)
    chrome = BrowserDriver(args.chrome)
    loop = asyncio.get_running_loop()
    chrome.launch()
    origin = "http://%s:%d" % (args.listen_addr, args.listen_port)
    tab = chrome.new_tab(origin + "/", loop)
    await tab.start()
    try:
        rc = await capture_flow(tab, args, salt, origin, summary)
        all_reqs = tab.all_requests()
        trace_rows.extend(all_reqs)
        return rc
    except Exception as exc:
        summary["fatal"] = redact(str(exc))
        traceback_print(exc)
        try:
            all_reqs = tab.all_requests()
            trace_rows.extend(all_reqs)
        except Exception:
            pass
        return 1
    finally:
        await _safe_aclose(tab)
        chrome.shutdown()


async def _safe_aclose(tab) -> None:
    try:
        await tab.aclose()
    except Exception:
        pass


def traceback_print(exc: BaseException) -> None:
    import traceback
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    sys.stderr.write("".join(tb[-6:]))


# urlparse é usado em route_bucket
from urllib.parse import urlparse  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
