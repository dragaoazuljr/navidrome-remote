#!/usr/bin/env python3
"""ws_probe.py (A2) — sondas WebSocket de transporte contra o v0.64.2,
com CLASSIFICAÇÃO DE TRANSPARÊNCIA (o coração da cláusula B(i)).

Por cada rota candidata, o handshake RFC6455 é tentado em (quando
habilitados) DOIS canais:

  * proxy  — o serviço único (o que a LAN vê)
  * direct — o upstream real (o que a LAN veria sem nós)

Classificação por candidata (proxy × direct):

  101 em algum canal ............ GREEN  (upgrade atravessa — ponte ok)
  !=101 EM AMBOS + iguais ....... MIRRORED (upstream recusa; nós
                                   espelhamos a recusa sem romper —
                                   RED *do fato*, não *nosso*)
  !=101 e DIVERGENTES .......... BROKEN (nossa proxy rompeu/sintetizou
                                   — VERMELHO da cláusula B(i))
  sem resposta em um canal ...... INCONCLUSIVE

Modo sessão (--cred-file): o cookie de sessão do Navidrome é obtido com
um POST /auth/login direto no upstream (1 tentativa — X-RateLimit: 5)
e anexado às sondas (algumas rotas podem só existir com sessão).
O VALOR do cookie nunca é impresso/escrevido — só o nome.

Uso:
  ws_probe.py [proxy_port] [--direct] [--upstream HOST:PORT]
              [--cred-file FICHEIRO] [--cookie "NOME=VALOR"]
              [--json-out CAMINHO]
Compatível com o embutimento do c1_probe.sh (argv[1] = porta do proxy).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_PROBE_KEY_MARK = "nvr-a2-probe"

# Candidatas: as 6 históricas do A1 + 2 palpites novos (eventstream da UI
# dev, updates). /api/v1/* é ROTA LOCAL nossa (não sobe ao upstream):
# marcada e excluída do espelho, mas sondada por completude.
CANDIDATES = [
    "/ws",
    "/app/ws",
    "/socket.io/?EIO=4&transport=websocket",
    "/events",
    "/sse",
    "/eventstream",
    "/updates",
    "/api/v1/events",
]
LOCAL_ONLY = ("/api/v1/events",)


async def probe_once(host: str, port: int, target: str,
                     cookie: "str | None", timeout: float = 6.0):
    """Um handshake; devolve dict(result, status, hdrs, body_len, echo)."""
    key = base64.b64encode(os.urandom(16)).decode()
    out = {
        "result": "err", "status": None, "hdrs": {},
        "body_len": 0, "echo": None, "detail": "",
    }
    try:
        rd, wr = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        out["detail"] = "conn-%s" % type(exc).__name__
        return out
    head = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
    )
    if cookie:
        head += f"Cookie: {cookie}\r\n"
    head += "\r\n"
    wr.write(head.encode())
    try:
        await asyncio.wait_for(wr.drain(), timeout)
        buf = bytearray()
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = await asyncio.wait_for(rd.read(4096), timeout)
            if not chunk:
                break
            buf.extend(chunk)
        if b"\r\n\r\n" not in buf:
            wr.close()
            out["detail"] = "no-head"
            return out
        head_bytes = bytes(buf[:buf.index(b"\r\n\r\n") + 4])
        first_line = head_bytes.split(b"\r\n", 1)[0].decode(
            "latin-1", "replace")
        status_txt = first_line.split(" ")[1] if " " in first_line else "?"
        try:
            status = int(status_txt)
        except ValueError:
            status = -1
        hdrs = {}
        for ln in head_bytes.split(b"\r\n")[1:-1]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                hdrs[k.decode("latin-1", "replace").strip().lower()] = (
                    v.decode("latin-1", "replace").strip())
        body_part = bytes(buf[buf.index(b"\r\n\r\n") + 4:])
        out.update({"status": status, "hdrs": hdrs,
                    "body_len": len(body_part)})
        if status == 101:
            out["result"] = "101"
            # eco: envia um frame e lê a resposta (ponte bidirecional)
            try:
                payload = WS_PROBE_KEY_MARK.encode()
                mask = os.urandom(4)
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                frame = bytes([0x81, 0x80 | len(masked)]) + mask + masked
                wr.write(frame)
                await asyncio.wait_for(wr.drain(), timeout)
                hb = await asyncio.wait_for(rd.readexactly(2), timeout)
                ln = hb[1] & 0x7F
                if ln == 126:
                    ln = int.from_bytes(
                        await rd.readexactly(2), "big")
                body = await asyncio.wait_for(rd.readexactly(ln), timeout)
                out["echo"] = body.decode("utf-8", "replace")[:64]
            except (OSError, asyncio.TimeoutError):
                out["echo"] = "101-eco-nao-verificado"
            wr.close()
            return out
        out["result"] = "HTTP-%d" % status
        wr.close()
        return out
    except (OSError, asyncio.TimeoutError) as exc:
        out["detail"] = type(exc).__name__
        try:
            wr.close()
        except (Exception,):
            pass
        return out


def classify(candidate: str, prox: dict, dire: "dict | None") -> dict:
    """Classificação da cláusula B(i) para uma candidata."""
    if prox.get("result") == "101":
        if dire is not None and dire.get("result") == "101":
            verdict = "GREEN (101 nos dois canais)"
            cls = "green"
        else:
            verdict = "GREEN (101 via proxy)"
            cls = "green"
        return {"cls": cls, "verdict": verdict}
    if prox.get("result") in (None, "err", "no-head") or prox.get("status") is None:
        return {
            "cls": "inconclusive",
            "verdict": "inconclusivo (%s)" % (prox.get("detail")
                                              or prox.get("result")),
        }
    if dire is None:
        return {
            "cls": "red-facto" if (prox.get("status") or 0) >= 400 else
            "red-facto",
            "verdict": ("upstream recusa (status %s); espelho não medido "
                        "(use --direct)" % prox.get("status")),
        }
    same = (prox.get("status") == dire.get("status"))
    if same:
        return {
            "cls": "mirrored",
            "verdict": ("upstream recusa nos dois canais (status %s) — "
                        "recursão espelhada SEM ROMPIMENTO (não é vermelho "
                        "nosso)" % prox.get("status")),
        }
    return {
        "cls": "broken",
        "verdict": ("BROKEN: proxy devolveu %s, direct devolveu %s — "
                    "ROMPIMENTO no handshake (VERMELHO B(i) nosso)"
                    % (prox.get("status"), dire.get("status"))),
    }


async def get_session_cookie(upstream_host: str, upstream_port: int,
                             cred_file: str) -> "str | None":
    """1x POST /auth/login direto no upstream; devolve Cookie header."""
    with open(cred_file, "r", encoding="utf-8") as fh:
        u, p = fh.readline().strip().split(":", 1)
    u, p = u.strip(), p
    payload = json.dumps({"username": u, "password": p}).encode()
    (status, _reason, hdrs, _body) = await _http_req(
        upstream_host, upstream_port, "POST", "/auth/login",
        headers={"Content-Type": "application/json"}, body=payload)
    if status not in (200, 201):
        return None
    for k, v in hdrs:
        if k.lower() == "set-cookie":
            return v.split(";")[0].strip()
    return None


async def _http_req(host, port, method, target, headers=None,
                    body=b"", timeout=8.0):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _http import req
    return await req(host, port, method, target, headers=headers,
                     body=body, timeout=timeout)


async def main_async(args) -> int:
    cookie = None
    if args.cookie:
        cookie = args.cookie  # operador forneceu; valor não é impresso
    elif args.cred_file:
        uh, _, up = args.upstream.partition(":")
        up = int(up or 4533)
        cookie = await get_session_cookie(uh, up, args.cred_file)
        if cookie and args.cookie_out:
            # 0600 file so reuso por outras suites da mesma corrida —
            # 1unico POST /auth/login por rodada (rate-limit 5)
            fd = os.open(args.cookie_out,
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(cookie + "\n")

    cookie_name = (cookie or "").split("=")[0] if cookie else None
    prox_rows = []
    for cand in CANDIDATES:
        p = await probe_once("127.0.0.1", args.proxy_port, cand, cookie)
        d = None
        if args.direct:
            uh, _, usp = args.upstream.partition(":")
            d = await probe_once(uh, int(usp or 4533), cand, cookie)
        cls = classify(cand, p, d)
        prox_rows.append({
            "candidate": cand,
            "local_only": cand in LOCAL_ONLY,
            "session_cookie": cookie_name,
            "proxy": {
                "result": p.get("result"), "status": p.get("status"),
                "detail": p.get("detail"),
                "hdrs_subset": {k: p.get("hdrs", {}).get(k)
                                for k in ("upgrade", "connection",
                                          "sec-websocket-accept",
                                          "access-control-allow-origin")
                                if p.get("hdrs", {}).get(k)},
                "body_len": p.get("body_len"), "echo": p.get("echo"),
            },
            "direct": None if d is None else {
                "result": d.get("result"), "status": d.get("status"),
                "detail": d.get("detail"),
                "hdrs_subset": {k: d.get("hdrs", {}).get(k)
                                for k in ("upgrade", "connection",
                                          "sec-websocket-accept",
                                          "access-control-allow-origin")
                                if d.get("hdrs", {}).get(k)},
                "body_len": d.get("body_len"),
            },
            "classify": cls,
        })

    # -------------------------------------------------------- markdown out
    print("| rota candidata | via proxy | direct | classificação |")
    print("|---|---|---|---|")
    for r in prox_rows:
        pv = "%s (%s)" % (r["proxy"]["result"], r["proxy"]["status"]) \
            if r["proxy"]["result"] else r["proxy"]["detail"]
        dv = "-"
        if r["direct"]:
            dv = "%s (%s)" % (r["direct"]["result"],
                              r["direct"]["status"])
        mark = " *" if r["local_only"] else ""
        print("| `%s`%s | %s | %s | %s |"
              % (r["candidate"].split("?")[0], mark, pv, dv,
                 r["classify"]["verdict"]))
    print()
    n_green = sum(1 for r in prox_rows
                  if r["classify"]["cls"] == "green")
    n_mirr = sum(1 for r in prox_rows
                 if r["classify"]["cls"] == "mirrored")
    n_brk = sum(1 for r in prox_rows
                if r["classify"]["cls"] == "broken")
    if n_green:
        print("**Cláusula B(i): VERDE** — %d candidata(s) com 101 "
              "atravessando o proxy." % n_green)
    elif n_brk:
        print("**Cláusula B(i): VERMELHO (nosso)** — %d rompipmento(s) no "
              "handshake causado pelo proxy." % n_brk)
    else:
        print("**Cláusula B(i): RED documentado** — nenhuma candidata com "
              "101; recusas espelhadas (não rompimento) em %d/sondadas. "
              "O v0.64.2 aparenta não operar WS nas rotas candidatas "
              "(dado novo p/ spec v1.3; decisão de fallback registrada "
              "em docs/decisions/)." % n_mirr)

    if args.json_out:
        doc = {
            "probe": "ws_probe-a2",
            "proxy": "127.0.0.1:%d" % args.proxy_port,
            "direct": args.upstream if args.direct else None,
            "session_cookie": cookie_name,
            "rows": prox_rows,
            "counts": {"green": n_green, "mirrored": n_mirr,
                       "broken": n_brk,
                       "total": len(prox_rows)},
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh_write(doc, fh)
    return 0


def fh_write(doc: dict, fh) -> None:
    json.dump(doc, fh, indent=2, ensure_ascii=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("proxy_port", nargs="?", type=int, default=8480)
    ap.add_argument("--direct", action="store_true",
                    help="sonda também o upstream direto (espelho)")
    ap.add_argument("--upstream", default="192.168.0.200:4533",
                    help="host:port do upstream (canal direct)")
    ap.add_argument("--cred-file",
                    default="",
                    help="credencial 0600 (user:pass): 1 POST /auth/login; "
                         "vazio = anonimo (preserva rate-limit)")
    ap.add_argument("--cookie", default="",
                    help="Cookie header explícito (precede --cred-file)")
    ap.add_argument("--no-session", action="store_true",
                    help="não tenta cookie de sessão")
    ap.add_argument("--cookie-out", default="",
                    help="grava o cookie obtido (0600) p/ reuso "
                         "por outras suites da rodada")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()
    if args.no_session:
        args.cred_file = ""
        args.cookie = ""
    rc = asyncio.run(main_async(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
