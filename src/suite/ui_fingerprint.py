#!/usr/bin/env python3
"""ui_fingerprint.py (A9) — FINGERPRINT DA GERACAO DE UI NO ORIGIN PUBLICADO.

Parte do gate A2 (run_gate.py, etapa G6): identifica a geraçao da UI
oficial do MESMO jeito que o bridge faz (feature-probe positivo, nunca
cega) e CORRELA com os canais black-box do G0 (/rest/ping +
__APP_CONFIG__ no HTML). Emissao maquina-leitura na ultima linha:

  SUMMARY={"origin": ..., "upstream": ..., "ping_version": ...,
           "html_cfg": {...}, "bridge": {...}, "crosscheck_ok": ...,
           "verdict": "green"|"red"}

Veredictos:
  green  classe `known` DO PROBE do bridge E versoes correlacionam
         (ping ~= declaracao; ausencia de versao declarada nao e
         contradicao — o G0 da o resto da confianca).
  red    bridge ausente (fatal: hook partido), classe `drift`
         (versao estrangeira) ou `unknown` (contrato da geracao nao
         verificavel = HOOK PARTIDO apos cambio de versao), OU
         contradicao bridge x black-box. O gate NAO aprova green-close
         com red (caminho registrado: ADR-002/007).

Sem chrome => o GATE degrada a 'pendente-QA' no lado do runner (este
modulo sai com rc=2 e sem SUMMARY: falha operacional, nao veredicto).

Uso (dev-ws):
  python3 suite/ui_fingerprint.py --listen-addr 127.0.0.1 --listen-port N
      [--vhost HOST] [--cred-file F] [--chrome BIN]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cdp import BrowserDriver  # noqa: E402
from player_boot_capture import (  # noqa: E402
    FORM_PROBE_JS,
    JS_PASSWORD_TMPL,
    JS_SUBMIT,
    JS_USERNAME_TMPL,
    js_auth_check,
    read_cred,
    subsonic_salt,
)

BRIDGE_CLASSIFY_JS = (
    "(function(){var b=window.__NVRA_BRIDGE__;"
    "return (b && b._t) ? b._t._classify() : null;})()"
)


def bb_get(url: str, vhost: str) -> "tuple[int, str]":
    hdr = {"User-Agent": "gate-a2-ui-fp/1"}
    if vhost:
        hdr["X-Forwarded-Host"] = vhost
    req = urllib.request.Request(url, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa
        return -1, repr(e)


def norm_parts(version: "str | None") -> "list[str]":
    """`0.64.2 (10114574)` / `v0.65.0-rc1` -> ['0','64','2'] (prefix)."""
    if not version:
        return []
    v = version.strip().lstrip("vV")
    v = v.split("(")[0].split("-")[0].strip()
    return [p for p in v.split(".") if p.isdigit()][:3]


def versions_compatible(a: "str | None", b: "str | None") -> bool:
    """Compatibilidade conservadora: mesma MAJOR e (quando ambos tem
    MINOR) mesmo MINOR. Ausencia de um lado nunca e contradicao — o
    outro canal do G0 responde pela confianca."""
    pa, pb = norm_parts(a), norm_parts(b)
    if not pa or not pb:
        return True
    if pa[0] != pb[0]:
        return False
    if len(pa) >= 2 and len(pb) >= 2:
        return pa[1] == pb[1]
    return True


def parse_html_config(html: str) -> "dict[str, str | None]":
    out: "dict[str, str | None]" = {"version": None, "build": None}
    m = re.search(r"__APP_CONFIG__\s*=\s*(\{.*?\})", html, re.S)
    if not m:
        return out
    blk = m.group(1)
    mv = re.search(r'\bversion["\']?\s*:\s*["\']([^"\']*)', blk)
    mb = re.search(r'\bbuild["\']?\s*:\s*["\']([^"\']*)', blk)
    out["version"] = mv.group(1) if mv else None
    out["build"] = mb.group(1) if mb else None
    return out


async def run(args: argparse.Namespace) -> int:
    origin = "http://%s:%d" % (args.listen_addr, args.listen_port)
    driver = BrowserDriver(args.chrome)
    driver.launch(port=None)
    loop = asyncio.get_running_loop()
    tab = driver.new_tab("about:blank", loop)
    await tab.start()  # conecta o WS CDP + habilita Page/Network/Runtime
    try:
        # -- 1) entrar no origin publicado (o proxy injeta o bridge) ----
        await tab.navigate(origin + "/", timeout=30)
        login_note = "anonimo"
        if args.cred_file:
            user, pw = read_cred(args.cred_file)
            salt = subsonic_salt(pw)
            form = await tab.eval(FORM_PROBE_JS)
            if not (form and form.get("pw")):
                await tab.navigate(origin + "/login", timeout=30)
                await asyncio.sleep(2.0)
                form = await tab.eval(FORM_PROBE_JS)
            if form and form.get("pw"):
                r1 = await tab.eval(JS_USERNAME_TMPL.replace(
                    "__USR__", json.dumps(user)))
                r2 = await tab.eval(JS_PASSWORD_TMPL.replace(
                    "__PWP__", json.dumps(salt)))
                ok = bool(r1 and r1.get("ok") and r2 and r2.get("ok"))
                if ok:
                    await tab.eval(JS_SUBMIT)
                    for _ in range(15):
                        await asyncio.sleep(1.0)
                        chk = await tab.eval(js_auth_check(pw))
                        if chk and chk.get("ok"):
                            login_note = "form-ok"
                            break
                    if login_note != "form-ok":
                        login_note = "form-fill-sem-autenticacao"
                else:
                    login_note = "fill-falhou: %s %s" % (r1, r2)
        # depois do login (ou no modo anonimo) garante a raiz da SPA
        # com sessao: a injecao do bridge acontece no HTML servido.
        await tab.navigate(origin + "/", timeout=30)

        # -- 2) esperar o bridge existir e dar sua classe --------------
        bridge: "dict | None" = None
        for _ in range(60):  # ate 30 s
            raw = await tab.eval(BRIDGE_CLASSIFY_JS)
            if isinstance(raw, dict):
                bridge = raw
                break
            await asyncio.sleep(0.5)

        # -- 3) canais black-box do G0 ---------------------------------
        stp, ping_body = bb_get(origin + "/rest/ping", args.vhost)
        sp, html_txt = bb_get(origin + "/", args.vhost)
        ping_m = re.search(r'serverVersion="([^"]+)"', ping_body)
        ping_version = ping_m.group(1) if ping_m else None
        html_cfg = parse_html_config(html_txt)
        declared = (bridge or {}).get("v")

        # -- 4) veredicto ----------------------------------------------
        crosscheck_ok: "bool | None" = None
        verdict = "red"
        reasons: "list[str]" = []
        if bridge is None:
            reasons.append("bridge-absent (hook fatalemente partido)")
        else:
            k = bridge.get("k")
            if k != "known":
                reasons.append("bridge-class=%s" % k)
            # correlacao bridge x black-box: a declaracao do bridge
            # (quando ha) deve casar com ping e com o HTML.
            cc = True
            for name, cand in (("ping", ping_version),
                               ("html-version", html_cfg.get("version"))):
                if cand is not None and declared is not None:
                    if not versions_compatible(str(declared), str(cand)):
                        cc = False
                        reasons.append(
                            "%s contradiz declaracao (%s vs %s)"
                            % (name, declared, cand))
            crosscheck_ok = (k == "known") and cc
            if not cc:
                reasons.append("contradicao bridge x black-box")
            verdict = "green" if (k == "known" and cc) else "red"
        # se nao conseguirmos nem o ping, o veredicto fica inconclusivo
        # para o lado green (mas segue red se ja estava red por bridge)
        if stp != 200 and verdict == "green":
            crosscheck_ok = None
            verdict = "red"
            reasons.append("ping indisponivel (st=%s)" % stp)

        doc = {
            "origin": origin,
            "upstream": args.upstream,
            "vhost": args.vhost or None,
            "login": login_note,
            "ping_http_status": stp,
            "ping_version": ping_version,
            "html_http_status": sp,
            "html_cfg": html_cfg,
            "bridge": bridge,
            "bridge_missing_reasons": reasons,
            "crosscheck_ok": crosscheck_ok,
            "verdict": verdict,
        }
        for line in reasons:
            print("[UI-FP] motivo-red: %s" % line)
        print("[UI-FP] verdict=%s (ping=%s html.version=%s)"
              % (verdict, ping_version, html_cfg.get("version")))
        print("SUMMARY=" + json.dumps(doc, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # noqa
        # falha operacional (navegacao/chrome), NAO veredicto: o runner
        # trata como 'pendente-QA' quando chrome ausente; aqui e erro.
        print("[UI-FP] ERRO: %r" % exc, file=sys.stderr)
        return 2
    finally:
        # o driver CHROME e deste processo (o SERVIDO e do runner do
        # gate); derrubar aqui sempre.
        try:
            driver.shutdown()
        except Exception:  # noqa
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listen-addr", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=8481)
    ap.add_argument("--upstream", default="192.168.0.200:4533")
    ap.add_argument("--vhost", default="")
    ap.add_argument("--cred-file", default="")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
