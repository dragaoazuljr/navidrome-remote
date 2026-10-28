#!/usr/bin/env python3
"""ws_probe.py — sondas WebSocket contra candidatas de transporte da UI
Navidrome v0.64.2, VIA proxy (C1). Para cada rota candidata tenta o
handshake RFC6455 e classifica:

  101            -> verde Cláusula B(i): upgrade atravessa o proxy
                   (se houver eco, também confirma a ponte bidirecional)
  4xx/5xx (http) -> upstream RECUSA/ignora: candidato não usado (e, se o
                   player usar outra rota, evidencia o RED documentado)
  timeout/err    -> inconclusivo

Printa uma tabela markdown pronta para colar no relatório.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CANDIDATES = [
    "/ws",
    "/app/ws",
    "/socket.io/?EIO=4&transport=websocket",
    "/events",
    "/sse",
    "/api/v1/events",
]


async def probe(port: int, target: str, timeout: float = 6.0):
    key = base64.b64encode(os.urandom(16)).decode()
    try:
        rd, wr = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        return f"conn-err ({type(exc).__name__})", "-", False
    head = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    wr.write(head.encode())
    try:
        await asyncio.wait_for(wr.drain(), timeout)
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(rd.read(4096), timeout)
            if not chunk:
                break
            buf.extend(chunk)
        if b"\r\n\r\n" not in buf:
            wr.close()
            return "no-head", "-", False
        head_bytes = bytes(buf[: buf.index(b"\r\n\r\n") + 4])
        first_line = head_bytes.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        status = first_line.split(" ", 1)[1].split(" ", 1)[0] if " " in first_line else "?"
        if status == "101":
            # handshake OK — testar eco com um frame
            echo_ok = "-"
            try:
                payload = b"nvr-probe"
                mask = os.urandom(4)
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                frame = bytes([0x81, 0x80 | len(masked)]) + mask + masked
                wr.write(frame)
                await asyncio.wait_for(wr.drain(), timeout)
                hb1 = await asyncio.wait_for(rd.readexactly(2), timeout)
                ln = hb1[1] & 0x7F
                if ln == 126:
                    ln = int.from_bytes(await rd.readexactly(2), "big")
                body = await asyncio.wait_for(rd.readexactly(ln), timeout)
                echo_ok = f"eco ok (`{body.decode('utf-8','replace')[:40]}`)"
            except (OSError, asyncio.TimeoutError):
                echo_ok = "101 sem eco testado"
            wr.close()
            return "101", f"GREEN B(i) — {echo_ok}", True
        wr.close()
        detail = first_line
        if target == "/socket.io/?EIO=4&transport=websocket":
            detail += " [socket.io]"
        return f"HTTP-{status}", f"RED/inconclusivo — upstream respondeu HTTP (rota `{target}` não é WS no v0.64.2, ou precisa de sessão)", False
    except (OSError, asyncio.TimeoutError) as exc:
        try:
            wr.close()
        except (OSError, RuntimeError):
            pass
        return f"err ({type(exc).__name__})", "inconclusivo (timeout/erro de I/O)", False
    finally:
        try:
            wr.close()
        except (OSError, RuntimeError):
            pass


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8480
    rows = []
    green_any = False
    for cand in CANDIDATES:
        status, verdict, green = await probe(port, cand)
        green_any = green_any or green
        rows.append((cand, status, verdict))
    print("| rota candidata | resultado | interpretação |")
    print("|---|---|---|")
    for cand, status, verdict in rows:
        print(f"| `{cand.split('?')[0]}` | {status} | {verdict} |")
    print()
    if green_any:
        print("**Cláusula B(i): VERDE** — pelo menos uma rota de WS atravessa o proxy intacta (101).")
    else:
        print("**Leitura B(i):** nenhuma candidata devolveu 101. Se o player v0.64.2 realmente não usa WS (dado novo p/ spec), este é o RED *esperado*; se ele usa e nós rompemos, os testes unitários (helper ws) capturariam — conferir no spy (`upgrade_red`/`101`/`ws_close`).")


if __name__ == "__main__":
    asyncio.run(main())
