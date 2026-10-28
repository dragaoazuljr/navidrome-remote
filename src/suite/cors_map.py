#!/usr/bin/env python3
"""cors_map.py (A2) — mapa CORS/OPTIONS por rota do v0.64.2 (cláusula A).

Mede a superfície CORS REAL do Navidrome, célula por célula:

  rotas      : conjunto fixo cobrindo REST, stream, estáticos e nossa RPC
  ctx        : anon | session (cookie via 1 POST /auth/login direto no
               upstream — o VALOR nunca sai; só o nome)
  origin     : (ausente) | self (127.0.0.1:porta-do-proxy) | cross
  variante   : GET · OPTIONS(ACRM:GET) · OPTIONS(ACRM:POST)
  canal      : direct (upstream) × proxy (nós) — mesma célula

Transparência (o ponto central da A2): para cada célula, compara-se o
pareamento NORMALIZADO (status + headers CORS/Vary/Age-independentes)
entre canal directo e canal proxy.

  espelhado=YES  → o proxy reproduz a resposta do upstream sem divergir
  espelhado=NO   → divergência (potencial vermelho de protocolo; o diff
                   detalha quais campos diferiram)

Saída: tabelas markdown (para o relatório A2) + JSON (para gate/baseline).
Sem dependências além da stdlib. Uso:

  cors_map.py [--listen-port P] [--upstream HOST:PORT] [--vhost V]
              [--cred-file PATH] [--cross-origin URL]
              [--only-routes csv] [--json-out PATH] [--md-out PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _http import header  # noqa: E402
import _http as H  # noqa: E402

# ---------------------------------------------------------------- constants
DEFAULT_ROUTES = [
    "/",
    "/app/",
    "/version",
    "/rest/ping",
    "/rest/about",
    "/rest/artist",
    "/rest/song",
    "/rest/user",
    "/stream",
    "/cover-art",
    "/playlists",
    "/queue",
    "/favicon.ico",
    "/api/v1/health",
    "/api/v1/play",
]

# Variantes de OPTIONS usadas para sondar a política de métodos CORS.
OPTIONS_ACRMS = ["GET", "POST"]

# Campos CORS relevantes para o pareamento normalizado.
CORS_FIELDS = [
    "access-control-allow-origin",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-max-age",
    "vary",
]

# Cabeçalhos voláteis — ignorados no diff de transparência.
VOLATILE_HEADERS = {"date", "etag", "last-modified", "age"}


# ----------------------------------------------------------------- helpers
def norm_cell(status: int, hdrs: dict, body_len: int) -> "dict[str, object]":
    """Normaliza uma célula para comparação cross-canal."""
    out: "dict[str, object]" = {
        "status": status,
        "body_len": min(body_len, 1 << 20),
    }
    for f in CORS_FIELDS:
        vals = [v for k, v in hdrs.items() if k.lower() == f]
        # vary pode acumular; ordena para estabilidade
        out[f] = sorted(vals) if vals else None
    return out


def cell_diff(a: dict, b: dict) -> list:
    diffs = []
    if a.get("status") != b.get("status"):
        diffs.append("status %s!=%s" % (a.get("status"), b.get("status")))
    for k in a:
        if k in ("status", "body_len"):
            continue
        va, vb = a.get(k), b.get(k)
        if va != vb:
            diffs.append("%s: %s != %s" % (k, va, vb))
    return diffs


# ------------------------------------------------------------------- cells
async def fetch_cell(host: str, port: int, route: str, ctx: str,
                     origin: Optional[str], acr_method: Optional[str],
                     cookie: Optional[str], vhost: str,
                     is_options: bool) -> dict:
    """Executa UMA célula e devolve dados brute-force."""
    headers = {"User-Agent": "navidrome-remote-suite/1"}
    if origin:
        headers["Origin"] = origin
    if acr_method:
        headers["Access-Control-Request-Method"] = acr_method
        headers["Access-Control-Request-Headers"] = "x-navidrome-test"
    if cookie:
        headers["Cookie"] = cookie
    if vhost:
        headers["Host"] = vhost
    method = "OPTIONS" if is_options else "GET"
    # Para GET sem sessão: alguns endpoints exigem credentials → sem origin
    status, reason, hdrs_raw, body = await H.req(
        host, port, method, route, headers=headers)
    hdrs = {}
    for k, v in hdrs_raw:
        lk = k.lower()
        if lk in VOLATILE_HEADERS:
            continue
        hdrs[lk] = v
    return {
        "route": route,
        "ctx": ctx,
        "origin": origin,
        "acr_method": acr_method,
        "channel": "%s:%d" % (host, port),
        "method": method,
        "status": status,
        "reason": reason,
        "body_len": len(body),
        "body_head": body[:300].decode("latin-1", "replace"),
        "headers": hdrs,
        "normalized": norm_cell(status, hdrs, len(body)),
    }


async def run_matrix(args, cookie: Optional[str]) -> dict:
    """Executa todas as células (direct + proxy)."""
    results = []
    origins = [None, args.self_origin, args.cross_origin]
    proxies = [("direct", args.upstream_host, args.upstream_port),
               ("proxy", "127.0.0.1", args.listen_port)]
    routes = [r.strip() for r in args.only_routes.split(",")] \
        if args.only_routes else DEFAULT_ROUTES

    total_cells = len(routes) * 2 * len(origins) * (1 + len(OPTIONS_ACRMS)) \
        * len(proxies)
    done = 0
    for route in routes:
        for ctx in ("anon", "session"):
            ck = cookie if ctx == "session" else None
            for origin in origins:
                # GET plain
                for ch_label, ch_host, ch_port in proxies:
                    cell = await fetch_cell(
                        ch_host, ch_port, route, ctx, origin,
                        None, ck, args.vhost, is_options=False)
                    cell["ctx_effective"] = ctx
                    cell["channel_label"] = ch_label
                    results.append(cell)
                    done += 1
                for acr in OPTIONS_ACRMS:
                    for ch_label, ch_host, ch_port in proxies:
                        cell = await fetch_cell(
                            ch_host, ch_port, route, ctx, origin,
                            acr, ck, args.vhost, is_options=True)
                        cell["ctx_effective"] = ctx
                        cell["channel_label"] = ch_label
                        results.append(cell)
                        done += 1
    return {"cells": results, "total_planned": total_cells,
            "total_executed": done}


# ---------------------------------------------------------------- pairing
def pair_channels(cells: list) -> list:
    """Empareia (direct, proxy) por chave estável e marca espelhamento."""
    pairs = []
    keyed = {}
    for c in cells:
        key = (c["route"], c["ctx_effective"],
               c["origin"] or "<none>",
               c["acr_method"] or "<get>")
        keyed.setdefault(key, {})[c["channel_label"]] = c
    for key, chans in keyed.items():
        d = chans.get("direct")
        p = chans.get("proxy")
        if d and p:
            diffs = cell_diff(d["normalized"], p["normalized"])
            pairs.append({
                "key": {"route": key[0], "ctx": key[1],
                        "origin": key[2], "acr": key[3]},
                "mirror": not diffs,
                "diffs": diffs,
                "direct_status": d["status"],
                "proxy_status": p["status"],
            })
    pairs.sort(key=lambda pp: (pp["key"]["route"], pp["key"]["ctx"],
                               pp["key"]["origin"], pp["key"]["acr"]))
    return pairs


# ------------------------------------------------------------------- output
def write_md(pairs: list, meta: dict, path: str) -> None:
    lines = []
    lines.append("## Mapa CORS/OPTIONS por rota — v0.64.2 (cláusula A)")
    lines.append("")
    lines.append("> Comparação direta (upstream) vs proxy (nós) para a mesma "
                 "célula (rota, contexto, origin, método).")
    lines.append("> `espelhado=NO` indica **divergência de protocolo** "
                 "(potencial vermelho). Detalhes no JSON.")
    lines.append("")
    lines.append("| rota | ctx | origin | acr | status D→P | espelhado | diffs |")
    lines.append("|------|-----|--------|-----|-----------|-----------|-------|")
    n_no = 0
    for pp in pairs:
        d_arrow = "%s→%s" % (pp["direct_status"], pp["proxy_status"])
        mark = "YES" if pp["mirror"] else "**NO**"
        diffs = "; ".join(pp["diffs"][:4]) or "-"
        if not pp["mirror"]:
            n_no += 1
        lines.append("| `%s` | %s | %s | %s | %s | %s | %s |" % (
            pp["key"]["route"], pp["key"]["ctx"],
            pp["key"]["origin"] or "∅",
            pp["key"]["acr"].upper() if pp["key"]["acr"] != "<get>" else "GET",
            d_arrow, mark, diffs))
    lines.append("")
    lines.append("**Totais:** %d pares medidos; %d espelhados; %d com "
                 "divergência." % (len(pairs), len(pairs) - n_no, n_no))
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_json(cells: list, pairs: list, meta: dict, path: str) -> None:
    slim = []
    for c in cells:
        slim.append({
            "route": c["route"], "ctx": c["ctx_effective"],
            "origin": c["origin"], "acr": c["acr_method"],
            "method": c["method"], "channel": c["channel_label"],
            "endpoint": c["channel"],
            "status": c["status"], "reason": c["reason"],
            "body_len": c["body_len"],
            "body_head": c["body_head"],
            "headers": {k: v for k, v in c["headers"].items()
                        if any(k.startswith(pref)
                               for pref in ("access-", "vary", "www-"
                                            ))},
            "normalized": c["normalized"],
        })
    doc = {
        "suite": "cors-map",
        "meta": meta,
        "cells": slim,
        "pairs": pairs,
        "mirror_stats": {
            "total_pairs": len(pairs),
            "mirrored": sum(1 for p in pairs if p["mirror"]),
            "diverged": sum(1 for p in pairs if not p["mirror"]),
        },
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------- cli
async def obtain_session_cookie(args) -> Optional[str]:
    """Sessão para ctx=session. Ordem:
      1) --session-cookie-file (cookie 0600 do ws_probe: ZERO POST);
      2) cred-file -> 1 POST /auth/login no upstream;
      3) senão None (ctx=session degenera em anon).
    Só o NOME do cookie chega ao relatório."""
    if getattr(args, "session_cookie_file", "") and \
            os.path.exists(args.session_cookie_file):
        with open(args.session_cookie_file, "r",
                  encoding="utf-8") as fh:
            line = fh.readline().strip()
        if "=" in line:
            return line
        return None
    if not args.cred_file or not os.path.exists(args.cred_file):
        return None
    from ws_probe import get_session_cookie
    return await get_session_cookie(
        args.upstream_host, args.upstream_port, args.cred_file)


async def async_main(args) -> int:
    uh, _, upp = args.upstream.partition(":")
    args.upstream_host = uh
    args.upstream_port = int(upp) if upp else 4533
    cookie = await obtain_session_cookie(args)
    meta = {
        "upstream": args.upstream,
        "vhost": args.vhost,
        "listen_port": args.listen_port,
        "self_origin": args.self_origin,
        "cross_origin": args.cross_origin,
        "session_cookie": (cookie or "").split("=")[0]
                          if cookie else None,
    }
    matrix = await run_matrix(args, cookie)
    pairs = pair_channels(matrix["cells"])
    if args.json_out:
        write_json(matrix["cells"], pairs, meta, args.json_out)
    if args.md_out:
        write_md(pairs, meta, args.md_out)
    if not args.quiet:
        ms = matrix
        print("células executadas: %d (plano %d)"
              % (ms["total_executed"], ms["total_planned"]))
        mir = sum(1 for p in pairs if p["mirror"])
        print("pares espelhados: %d / %d"
              % (mir, len(pairs)))
        bad = [p for p in pairs if not p["mirror"]]
        if bad:
            print("DIVERGÊNCIAS:")
            for p in bad[:20]:
                print("  %s [%s/%s/%s]: %s" % (
                    p["key"]["route"], p["key"]["ctx"],
                    p["key"]["origin"], p["key"]["acr"],
                    "; ".join(p["diffs"][:3])))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="cors-map (A2)")
    ap.add_argument("--listen-port", type=int, default=8480,
                    help="porta local do proxy (canal proxy)")
    ap.add_argument("--upstream", default="192.168.0.200:4533",
                    help="upstream real (canal direct)")
    ap.add_argument("--vhost", default="",
                    help="Host virtual (vazio = derivado)")
    ap.add_argument("--cred-file",
                    default="",
                    help="credencial 0600 (user:pass) p/ ctx=session; "
                         "vazio = sem contexto de sessao")
    ap.add_argument("--session-cookie-file", default="",
                    help="cookie 0600 reutilizado (ZERO POST; sai do "
                         "ws_probe da mesma rodada)")
    ap.add_argument("--self-origin",
                    default="http://127.0.0.1:8480",
                    help="origin self (ajuste à porta real)")
    ap.add_argument("--cross-origin",
                    default="http://192.168.0.50:5173",
                    help="origin externa hipotética (consumer app)")
    ap.add_argument("--only-routes", default="",
                    help="CSV para reduzir o conjunto (debug)")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--md-out", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    args.self_origin = args.self_origin.replace(
        ":8480", ":%d" % args.listen_port) \
        if args.self_origin.endswith(":8480") else args.self_origin
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
