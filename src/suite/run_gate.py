#!/usr/bin/env python3
"""run_gate.py (A2) — GATE DE VALIDAÇÃO DE UPGRADE, ancorado em v0.64.2.

Registra a suíte A2 como UM comando (gate), com veredicto automático:

  G0  âncora: versão black-box (env /rest/ping + __APP_CONFIG__ no HTML)
      vs ANCOR=0.64.2. Divergiu => DRIFT (resultado com confiança reduzida;
      o gate não promove).
  G1  regressão: suíte unittest do repo (A1 + testes A2) — offline.
  G2  cors_map.py: mapa CORS/OPTIONS por rota, canal directo×proxy,
      paridade normalizada (cláusula A / extensão de superfície).
  G3  ws_probe.py: cláusula B(i) — 101? espelhado? rompimento? (JSON out).
  G4  player_boot_capture.py (chrome headless): cláusula B(ii) — sessão
      Sob reload/navegação no ORIGIN PUBLICADO (listener do proxy); sem
      chrome => degrada para 'pendente-QA'.
  G5  baseline: diff vs GATE-DATA.json anterior; classes NONE/INFO/
      MAJOR/BREAKING (novo 101 ou nova versão => BREAKING: re-spec).

Auto-levanta o serviço (padrão): sobe fake mpv sink + `python -m
navidrome_remote` numa PORTA LIVRE (mesmo esquema do c1_probe.sh) e
derriba no fim — ou --attach para anexar a uma instância já no ar.

VEREDICTO FINAL:
  SPIKE CLOSED (green)     B(i)==VERDE && B(ii)==VERDE && âncora OK
  SPIKE CLOSED (fallback)  caso contrário: ADR-002 (ajuste cirúrgico em
                           v0.64.2: sem expectativa de WS; transporte
                           HTTP/JSON-IPC) é o caminho REGISTRADO.

Uso (dev-ws, user hermes):
  python3 suite/run_gate.py [--base-url http://LXC:4533] [--listen-port N]
      [--cred-file F] [--chrome BIN] [--attach] [--skip-browser]
      [--baseline-anterior GATE-DATA.json] [--out-root DIR]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUITE_PARENT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))   # src/
REPO_ROOT = os.path.abspath(os.path.join(SUITE_PARENT, ".."))    # repo
PY = sys.executable

ANCHOR_VERSION = "0.64.2"
DEFAULT_BASE_URL = "http://192.168.0.200:4533"   # LXC 9102, rota P1
DEFAULT_LISTEN_PORT = 8481
DEFAULT_CHROME = "/usr/bin/google-chrome"
# A0.1.3: convencao /tmp/nr-a2-creds APOSENTADA.
# --cred-file vazia => G3/G4 anonimos (fallback ADR-002); B(ii) nao avaliado.
DEFAULT_CRED = ""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------- G0
def g0_anchor(base_url: str) -> dict:
    """Versão black-box: (1) serverVersion no envelope /rest/ping;
    (2) version no __APP_CONFIG__ do HTML /. Nunca toca em credenciais."""
    out = {"version": None, "source": None,
           "serverVersion": None, "pageVersion": None, "match": False}
    import re
    import urllib.error
    import urllib.request

    def get(url: str, timeout: float = 8.0) -> "tuple[int, str]":
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "gate-a2/1"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            return -1, repr(e)

    st, body = get(base_url + "/rest/ping")
    m = re.search(r'serverVersion="([^"]+)"', body)
    if m:
        out["serverVersion"] = m.group(1)
        out["version"] = m.group(1)
        out["source"] = "/rest/ping (envelope)"
    st2, html = get(base_url + "/")
    pm = re.search(r'__APP_CONFIG__\s*=\s*\{.*?"version":"([^"]+)"', html, re.S)
    if pm:
        out["pageVersion"] = pm.group(1)
        if not out["version"]:
            out["version"] = pm.group(1)
            out["source"] = "/ (__APP_CONFIG__.version)"
    out["match"] = bool(out["version"]) and \
        (out["version"].startswith(ANCHOR_VERSION)
         or out["version"] == ANCHOR_VERSION)
    return out


# ------------------------------------------------------------------- G1
def g1_units(timeout: int = 900) -> dict:
    cmd = [PY, "-m", "unittest", "discover",
           "-s", "tests", "-t", ".", "-v"]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [SUITE_PARENT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    p = subprocess.run(cmd, cwd=SUITE_PARENT, env=env,
                       capture_output=True, text=True, timeout=timeout)
    tail = ((p.stdout or "") + ("\n" + (p.stderr or "")))[-2000:]
    return {"exit_code": p.returncode, "ok": p.returncode == 0,
            "tail": tail}


# ------------------------------------------------------------------- G2
def g2_cors(work: str, listen_port: int, upstream: str, vhost: str,
            cred_file: str, session_cookie: str) -> dict:
    jout = os.path.join(work, "cors-map.json")
    mout = os.path.join(work, "cors-map.md")
    cmd = [PY, os.path.join(SCRIPT_DIR, "cors_map.py"),
           "--listen-port", str(listen_port),
           "--upstream", upstream, "--vhost", vhost,
           "--cred-file", cred_file,
           "--session-cookie-file", session_cookie,
           "--json-out", jout, "--md-out", mout]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    doc = None
    if os.path.exists(jout):
        with open(jout, encoding="utf-8") as f:
            doc = json.load(f)
    return {"exit_code": p.returncode,
            "stdout_tail": (p.stdout or "")[-800:],
            "doc": doc,
            "mirror_stats": (doc or {}).get("mirror_stats")}


# ------------------------------------------------------------------- G3
def g3_ws(work: str, listen_port: int, upstream: str, cred_file: str,
          with_session: bool, cookie_out: str) -> dict:
    jout = os.path.join(work, "ws-probe.json")
    cmd = [PY, os.path.join(SCRIPT_DIR, "ws_probe.py"),
           str(listen_port), "--direct",
           "--upstream", upstream,
           "--cookie-out", cookie_out,
           "--json-out", jout]
    if with_session and os.path.exists(cred_file):
        cmd += ["--cred-file", cred_file]
    else:
        cmd += ["--no-session"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    doc = None
    if os.path.exists(jout):
        with open(jout, encoding="utf-8") as f:
            doc = json.load(f)
    return {"exit_code": p.returncode,
            "stdout_tail": (p.stdout or "")[-1200:],
            "doc": doc,
            "counts": (doc or {}).get("counts")}


# ------------------------------------------------------------------- G4
def g4_boot(work: str, listen_port: int, upstream: str, vhost: str,
            cred_file: str, chrome: str, skip: bool) -> dict:
    if skip:
        return {"skipped": True, "bii_verdict": "pendente-QA"}
    if not os.path.exists(chrome):
        return {"skipped": True, "bii_verdict": "pendente-QA",
                "reason": "chrome ausente"}
    cmd = [PY, os.path.join(SCRIPT_DIR, "player_boot_capture.py"),
           "--listen-addr", "127.0.0.1",
           "--listen-port", str(listen_port),
           "--upstream", upstream, "--vhost", vhost,
           "--cred-file", cred_file, "--chrome", chrome,
           "--out-dir", work, "--attach"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    res = {"exit_code": p.returncode,
           "stdout_tail": (p.stdout or "")[-1500:],
           "stderr_tail": (p.stderr or "")[-800:]}
    summary = None
    for fn in sorted(os.listdir(work)):
        if fn.startswith("a2-") and fn.endswith(".json"):
            with open(os.path.join(work, fn), encoding="utf-8") as f:
                candidate = json.load(f)
            if "bii" in candidate:
                summary = candidate
                break
    res["summary"] = summary
    bii = (summary or {}).get("bii") or {}
    res["bii_verdict"] = ("VERDE" if bii.get("green")
                          else ("RED" if "green" in bii
                                else "pendente-QA"))
    return res


# ------------------------------------------------------------------- G5
def _key_metrics(gd: dict) -> dict:
    g0, g2, g3, g4 = (gd.get("g0_anchor") or {},
                      gd.get("g2_cors") or {},
                      gd.get("g3_ws") or {},
                      gd.get("g4_boot") or {})
    doc3 = g3.get("doc") or {}
    doc2 = g2.get("doc") or {}
    return {
        "version": g0.get("version"),
        "ws_counts": g3.get("counts"),
        "ws_101_candidates": sorted({
            r.get("candidate") for r in doc3.get("rows", [])
            if (r.get("proxy") or {}).get("result") == "101"
            and r.get("candidate")}),
        "cors_mirror": g2.get("mirror_stats"),
        "cors_diverged_routes": sorted({
            (p.get("key") or {}).get("route")
            for p in doc2.get("pairs", []) if not p.get("mirror")}
            - {None}),
        "bii": g4.get("bii_verdict"),
    }


def g5_baseline(curr: dict, prev_path: str) -> dict:
    if not prev_path or not os.path.exists(prev_path):
        return {"baseline": prev_path or None, "class": "NONE",
                "notes": ["primeira fixacao (sem baseline anterior)"],
                "prev": None, "curr": _key_metrics(curr)}
    with open(prev_path, encoding="utf-8") as f:
        prev = json.load(f)
    cm, cp = (_key_metrics(prev), _key_metrics(curr))
    notes, klass = [], "NONE"
    if cm.get("version") and cp.get("version") \
            and cm["version"] != cp["version"]:
        klass = "BREAKING"
        notes.append("versao mudou: %s -> %s (re-spike)"
                     % (cm["version"], cp["version"]))
    if cp.get("ws_101_candidates") and not cm.get("ws_101_candidates"):
        klass = "BREAKING"
        notes.append("NOVAS rotas 101 surgiram: %s (re-spec!)"
                     % cp["ws_101_candidates"])
    for k in ("ws_counts", "cors_mirror", "bii"):
        if cm.get(k) != cp.get(k):
            if klass not in ("BREAKING",):
                klass = "MAJOR" if k in ("ws_counts", "bii") else "INFO"
            notes.append("%s: %s -> %s" % (k, cm.get(k), cp.get(k)))
    if cm.get("cors_diverged_routes") != cp.get("cors_diverged_routes"):
        if klass not in ("BREAKING",):
            klass = "MAJOR"
        notes.append("mapa de divergencia CORS mudou")
    return {"baseline": prev_path, "class": klass, "notes": notes,
            "prev": cm, "curr": cp}


# -------------------------------------------------------------- veredictos
def compute_verdicts(anchor_ok: bool, g3: dict, g4: dict) -> dict:
    cnt = g3.get("counts") or {}
    if g3.get("skipped"):
        bi_i = "inconclusivo (suite pulada)"
    elif not cnt:
        bi_i = "inconclusivo"
    elif cnt.get("green"):
        bi_i = "VERDE"
    elif cnt.get("broken"):
        bi_i = "VERMELHO-NOS (rompimento no handshake)"
    else:
        bi_i = "RED-facto (recusas espelhadas; v0.64.2 sem 101)"
    bi_ii = g4.get("bii_verdict")
    closed_green = (anchor_ok and bi_i == "VERDE" and bi_ii == "VERDE")
    return {
        "anchor": anchor_ok,
        "B(i)_ws": bi_i,
        "B(ii)_sessao": bi_ii,
        "spike_closed_green": closed_green,
        "spike_closed_via_fallback": not closed_green,
        "fallback_decision_ref":
            "src/docs/decisions/2026-10-28-adr-002-fallback-transport.md",
    }


# --------------------------------------------------------------- packaging
def render_report(gd: dict, ver: dict, g5res: dict, ts: str) -> str:
    g0 = gd.get("g0_anchor") or {}
    doc2 = (gd.get("g2_cors") or {}).get("doc") or {}
    doc3 = (gd.get("g3_ws") or {}).get("doc") or {}
    boot = gd.get("g4_boot") or {}
    L = []
    ap = L.append
    ap("# A2 — GATE DE VALIDAÇAO DE UPGRADE — %s" % ts)
    ap("")
    ap("_Execucao: dev-ws x86 contra LXC 9102 (rota P1, .0/24). "
       "Ancora do spike: **v%s**._" % ANCHOR_VERSION)
    ap("")
    ap("## 0. Ancora (black-box)")
    ap("")
    ap("| fonte | valor |")
    ap("|---|---|")
    ap("| /rest/ping `serverVersion` | `%s` |" % (g0.get("serverVersion")))
    ap("| pagina / `__APP_CONFIG__.version` | `%s` |"
       % (g0.get("pageVersion")))
    ap("| coincide com %s? | **%s** |"
       % (ANCHOR_VERSION, "SIM" if ver["anchor"] else "**NÃO (DRIFT)**"))
    ap("")
    ap("## 1. Cláusula B(i) — WebSocket atravessando o proxy")
    ap("")
    cnt = (gd.get("g3_ws") or {}).get("counts") or {}
    ap("Contagens: green=`%s` mirrored=`%s` broken=`%s` total=`%s`"
       % (cnt.get("green"), cnt.get("mirrored"), cnt.get("broken"),
          cnt.get("total")))
    ap("")
    ap("| rota candidata | via proxy | direct | classificacao |")
    ap("|---|---|---|---|")
    for r in doc3.get("rows", []):
        pv = "%s (%s)" % (r["proxy"]["result"], r["proxy"]["status"])
        dv = ("- %s (%s)"
              % (r["direct"]["result"], r["direct"]["status"])
              ) if r.get("direct") else "-"
        star = " *(rota local)*" if r.get("local_only") else ""
        ap("| `%s`%s | %s | %s | %s |"
           % (r["candidate"].split("?")[0], star, pv, dv,
              r["classify"]["verdict"]))
    ap("")
    ap("## 2. Cláusula A — mapa CORS/OPTIONS (paridade directo×proxy)")
    ap("")
    ms = (gd.get("g2_cors") or {}).get("mirror_stats") or {}
    ap("Pares: `%s` | espelhados: `%s` | divergentes: `%s`"
       % (ms.get("total_pairs"), ms.get("mirrored"), ms.get("diverged")))
    ap("")
    bad = [p for p in doc2.get("pairs", []) if not p.get("mirror")]
    if bad:
        ap("| rota | ctx | origin | acr | status D→P | diffs |")
        ap("|---|---|---|---|---|---|")
        for p in bad[:30]:
            ap("| `%s` | %s | %s | %s | %s→%s | %s |"
               % (p["key"]["route"], p["key"]["ctx"],
                  p["key"]["origin"] or "∅",
                  p["key"]["acr"].upper()
                  if p["key"]["acr"] != "<get>" else "GET",
                  p["direct_status"], p["proxy_status"],
                  "; ".join(p["diffs"][:3])))
    else:
        ap("**Todas as celas espelhadas** (proxy sem divergencia CORS "
           "medida).")
    ap("")
    ap("## 3. Cláusula B(ii) — sessão sob reload/navegacao no origin publicado")
    ap("")
    ap("Veredicto: **%s**" % boot.get("bii_verdict"))
    summ = boot.get("summary") or {}
    bii = summ.get("bii") or {}
    if bii.get("steps"):
        ap("")
        ap("| passo | auth | form voltou? | cookies apos (nomes) |")
        ap("|---|---|---|---|")
        for s in bii["steps"]:
            ap("| %s | %s | %s | %s |"
               % (s.get("step"), s.get("authStatus"),
                  s.get("loginFormBack"),
                  ", ".join(s.get("cookiesAfter") or []) or "-"))
    ap("")
    ap("## 4. Boot do player (captura real, chrome headless)")
    ap("")
    if summ:
        ap("- login: %s (%s)"
           % ((summ.get("login") or {}).get("ok"),
              (summ.get("login") or {}).get("via")))
        ap("- rotas no boot: %s"
           % (", ".join(sorted((summ.get("boot") or {}).get("routes", {}).keys()))
              or "-"))
        tidle = (summ.get("transport_idle") or {})
        ap("- transporte em repouso: %s" % (tidle.get("verdict") or "-"))
        tplay = (summ.get("transport_play") or {})
        if tplay.get("verdict"):
            ap("- transporte apos play: %s" % tplay["verdict"])
        brdg = (summ.get("bridge") or {})
        ap("- bridge.js executou no navegador: %s"
           % brdg.get("loaded_in_browser"))
    else:
        ap("- bootstrap indisponivel neste gate (%s)"
           % (boot.get("reason") or boot.get("skipped")))
    ap("")
    ap("## 5. Baseline (diff vs execucao anterior)")
    ap("")
    ap("Classe: **%s**" % g5res.get("class"))
    for n in g5res.get("notes", []):
        ap("- %s" % n)
    ap("")
    ap("## 6. VEREDICTO FINAL")
    ap("")
    ap("| item | valor |")
    ap("|---|---|")
    ap("| ancora v%s | %s |"
       % (ANCHOR_VERSION, "OK" if ver["anchor"] else "**DRIFT**"))
    ap("| B(i) WebSocket | %s |" % ver["B(i)_ws"])
    ap("| B(ii) sessao | %s |" % ver["B(ii)_sessao"])
    ap("| SPIKE CLOSED (green) | **%s** |"
       % ("SIM" if ver["spike_closed_green"] else "NAO"))
    ap("| fechamento via fallback registrado | %s → %s |"
       % ("SIM" if ver["spike_closed_via_fallback"] else "-",
          ver["fallback_decision_ref"]))
    ap("")
    ap("> Sem os dois verdes, o spike fecha pelo CAMINHO REGISTRADO: "
       "ajuste cirurgico ancorado em v0.64.2 (transporte HTTP/JSON-IPC, "
       "sem esperanca de WS) — ver ADR-002.")
    return "\n".join(L) + "\n"


def write_package(out_root: str, ts: str, gd: dict, ver: dict,
                  g5res: dict) -> "tuple[str, str]":
    pkg = os.path.join(out_root, "a2-gate-%s" % ts)
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "GATE-DATA.json"), "w",
              encoding="utf-8") as f:
        json.dump(gd, f, indent=2, ensure_ascii=False, default=str)
    with open(os.path.join(pkg, "GATE-REPORT.md"), "w",
              encoding="utf-8") as f:
        f.write(render_report(gd, ver, g5res, ts))
    z = os.path.join(pkg, "a2-gate-%s.zip" % ts)
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("GATE-DATA.json", "GATE-REPORT.md", "cors-map.md",
                     "cors-map.json", "ws-probe.json"):
            fp = os.path.join(pkg, name)
            if os.path.exists(fp):
                zf.write(fp, arcname=name)
    return (z, os.path.join(pkg, "GATE-DATA.json"))


# ----------------------------------------------------------- auto-levante
class ServiceBringup:
    """Espelho do c1_probe.sh: fake sink + servico unico numa porta livre."""

    def __init__(self, upstream: str, vhost: str, cred_file: str):
        self.tmpd = None
        self.srv = None
        self.sink = None
        self.port = None
        self.cred = (None, None)
        self.upstream = upstream
        self.vhost = vhost
        self.cred_file = cred_file

    def _read_cred(self) -> "tuple[str, str]":
        with open(self.cred_file, "r", encoding="utf-8") as fh:
            u, p = fh.readline().strip().split(":", 1)
        return u.strip(), p

    def start(self, wanted_port: int = 0) -> int:
        uh, _, up = self.upstream.partition(":")
        self.tmpd = tempfile.mkdtemp(prefix="nvg-")
        sock = os.path.join(self.tmpd, "mock.sock")
        spy = os.path.join(self.tmpd, "spy.ndjson")
        slog = os.path.join(self.tmpd, "srv.log")
        self.sink_log = os.path.join(self.tmpd, "sink.log")
        self.port = wanted_port or _free_port()
        self.sink = subprocess.Popen(
            [PY, os.path.join(SUITE_PARENT, "tools", "fake_mpv_sink.py"),
             "--socket", sock],
            stdout=open(self.sink_log, "wb"), stderr=subprocess.STDOUT)
        env = dict(os.environ)
        env.update({
            "NVRA_LISTEN_ADDR": "127.0.0.1",
            "NVRA_LISTEN_PORT": str(self.port),
            "NVRA_UPSTREAM": self.upstream,
            "NVRA_UPSTREAM_VHOST": self.vhost,
            "NVRA_MPV_SOCKET": sock,
            "NVRA_SPY_FILE": spy,
            "NVRA_API_TOKEN": "",
            "NVRA_NAV_USER": "",
            "NVRA_NAV_PASS": "",
        })
        if os.path.exists(self.cred_file):
            u, p = self._read_cred()
            self.cred = (u, p)
            env["NVRA_NAV_USER"] = u
            env["NVRA_NAV_PASS"] = p
        self.spypath = spy
        self.srv = subprocess.Popen(
            [PY, "-m", "navidrome_remote"],
            cwd=SUITE_PARENT, env=env,
            stdout=open(slog, "wb"), stderr=subprocess.STDOUT)
        self.srvlog = slog
        return self.port

    def wait_health(self, seconds: float = 25.0) -> bool:
        import urllib.request
        url = "http://127.0.0.1:%d/api/v1/health" % self.port
        dl = time.time() + seconds
        while time.time() < dl:
            try:
                req = urllib.request.Request(url, method="GET",
                                             headers={"User-Agent": "gate"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def stop(self) -> None:
        for proc in (self.srv, self.sink):
            if proc:
                try:
                    proc.terminate()
                except OSError:
                    pass
        for proc in (self.srv, self.sink):
            if proc:
                try:
                    proc.wait(4)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                except OSError:
                    pass
        if self.tmpd:
            shutil.rmtree(self.tmpd, ignore_errors=True)


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="A2: gate de validacao de upgrade (ancora v%s)"
        % ANCHOR_VERSION)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help="URL direta do Navidrome (G0 black-box)")
    ap.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT,
                    help="porta do listener (0 = porta livre; usado com "
                         "--attach quando fixa)")
    ap.add_argument("--upstream", default=DEFAULT_BASE_URL
                    .replace("http://", ""),
                    help="upstream (mesmo que base-url na pratica)")
    ap.add_argument("--vhost", default="",
                    help="vhost (P2 tailnet; vazio = P1)")
    ap.add_argument("--cred-file", default=DEFAULT_CRED)
    ap.add_argument("--chrome", default=DEFAULT_CHROME)
    ap.add_argument("--attach", action="store_true",
                    help="anexar a instancia ja no ar (nao auto-levantar)")
    ap.add_argument("--skip-browser", action="store_true")
    ap.add_argument("--skip-cors", action="store_true")
    ap.add_argument("--skip-ws", action="store_true")
    ap.add_argument("--baseline-anterior", default="")
    ap.add_argument("--out-root", default=os.path.join(
        REPO_ROOT, "src", "docs", "reports"))
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if not args.cred_file:
        print("[GATE] sem --cred-file: G3/G4 anonimos (fallback ADR-002); "
              "B(ii) nao avaliado nesta rodada.", file=sys.stderr)
    os.makedirs(args.out_root, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%d-%H%M%SZ")
    work = os.path.join(args.out_root, "work-%s" % ts)
    os.makedirs(work, exist_ok=True)

    gd: "dict[str, object]" = {
        "ts": ts, "anchor_expected": ANCHOR_VERSION,
        "upstream": args.upstream, "vhost": args.vhost}

    bring = ServiceBringup(args.upstream, args.vhost, args.cred_file)
    listen_port = args.listen_port
    session_cookie = os.path.join(work, "session.cookies")

    def _purge_session_cookie() -> None:
        # valor de sessao: sobrescrever e remover (jamais vai ao repo)
        try:
            if os.path.exists(session_cookie):
                with open(session_cookie, "w", encoding="utf-8") as fh:
                    fh.write("\0" * 256)
                os.unlink(session_cookie)
        except OSError:
            pass

    if not args.attach:
        listen_port = bring.start(args.listen_port)
        if not bring.wait_health():
            tail = ""
            try:
                with open(bring.srvlog, encoding="utf-8",
                          errors="replace") as f:
                    tail = f.read()[-1200:]
            except OSError:
                pass
            bring.stop()
            print("FATAL: servico nao ficou healthy em :%s\n%s"
                  % (listen_port, tail), file=sys.stderr)
            return 2
        print("[gate] servico+sink no ar em 127.0.0.1:%s (spy %s)"
              % (listen_port, bring.spypath))
    else:
        listen_port = args.listen_port
        print("[gate] anexando a instancia em 127.0.0.1:%s" % listen_port)

    try:
        print("[G0] ancora black-box...", flush=True)
        gd["g0_anchor"] = g0_anchor(args.base_url)
        print("    version=%s match=%s"
              % (gd["g0_anchor"]["version"],
                 gd["g0_anchor"]["match"]))

        print("[G1] suite unit (regressao)...", flush=True)
        gd["g1_units"] = g1_units()
        print("    ok=%s (rc=%s)"
              % (gd["g1_units"]["ok"], gd["g1_units"]["exit_code"]))

        # G3 ANTES do G2: ws_probe e o UNICO login direto da rodada
        # (escreve o cookie 0600); cors_map o reutiliza (zero POST).
        print("[G3] ws_probe (login unico da rodada)...", flush=True)
        gd["g3_ws"] = ({"skipped": True} if args.skip_ws
                       else g3_ws(work, listen_port, args.upstream,
                                  args.cred_file,
                                  with_session=not args.skip_browser,
                                  cookie_out=session_cookie))
        print("    counts=%s" % (gd["g3_ws"].get("counts")))

        print("[G2] cors_map (reusa cookie; zero POST)...", flush=True)
        gd["g2_cors"] = ({
        } if args.skip_cors
            else g2_cors(work, listen_port, args.upstream, args.vhost,
                         args.cred_file, session_cookie))
        if not args.skip_cors:
            print("    mirror=%s" % (gd["g2_cors"].get("mirror_stats")))

        print("[G4] player boot capture (chrome)...", flush=True)
        gd["g4_boot"] = g4_boot(work, listen_port, args.upstream,
                                args.vhost, args.cred_file, args.chrome,
                                skip=args.skip_browser)
        print("    bii=%s" % (gd["g4_boot"].get("bii_verdict")))

        print("[G5] baseline...", flush=True)
        gd["g5_baseline"] = g5_baseline(gd, args.baseline_anterior)
        print("    class=%s" % (gd["g5_baseline"].get("class")))

        ver = compute_verdicts(bool(gd["g0_anchor"]["match"]),
                               gd["g3_ws"], gd["g4_boot"])
        gd["verdict"] = ver
    finally:
        _purge_session_cookie()
        if not args.attach:
            bring.stop()

    zippath, datapath = write_package(args.out_root, ts, gd,
                                      gd["verdict"],
                                      gd["g5_baseline"])
    print("\n========= VEREDICTO =========")
    for k in ("anchor", "B(i)_ws", "B(ii)_sessao",
              "spike_closed_green", "spike_closed_via_fallback"):
        print("  %-26s %s" % (k + ":", ver[k]))
    print("\n  pacote  : %s" % zippath)
    print("  baselina: %s" % datapath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
