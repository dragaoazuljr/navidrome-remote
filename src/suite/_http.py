"""_http.py — helper de pedido HTTP/1.1 bruto (stdlib, asyncio).

Usado por cors_map.py / ws_probe.py (A2): precisamos controlar bytes
(cruciais para medir transparência), não conveniências de urllib.

Retorna (status, reason, headers[list[tuple]], body_limite). Body é
lido até MAX_BODY (o interesse é header/shape, não payload).
"""

from __future__ import annotations

import asyncio

MAX_BODY = 1 << 20  # 1 MiB de teto — respostas de erro/JSON são pequenas


async def req(host: str, port: int, method: str, target: str,
              headers=None, body: bytes = b"", timeout: float = 8.0):
    """Um pedido HTTP/1.1; devolve (status, reason, hdrs, body_prefix)."""
    hs = dict(headers or {})
    if not any(k.lower() == "host" for k in hs):
        hs["Host"] = "%s:%d" % (host, port)
    if body and not any(k.lower() == "content-length" for k in hs):
        hs["Content-Length"] = str(len(body))
    wire = ("%s %s HTTP/1.1\r\n" % (method, target)).encode()
    wire += "".join("%s: %s\r\n" % (k, v) for k, v in hs.items()).encode()
    wire += b"\r\n" + body
    try:
        rd, wr = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        return (-1, "conn-err:%s" % type(exc).__name__, [], b"")
    wr.write(wire)
    try:
        await asyncio.wait_for(wr.drain(), timeout)
    except (OSError, asyncio.TimeoutError):
        pass
    buf = bytearray()
    try:
        while b"\r\n\r\n" not in buf:
            c = await asyncio.wait_for(rd.read(8192), timeout)
            if not c:
                break
            buf.extend(c)
    except asyncio.TimeoutError:
        pass
    if b"\r\n\r\n" not in buf:
        wr.close()
        return (-1, "no-head", [], b"")
    idx = buf.index(b"\r\n\r\n")
    lines = bytes(buf[:idx]).decode("latin-1", "replace").split("\r\n")
    parts = lines[0].split(" ", 2)
    try:
        status = int(parts[1])
    except (IndexError, ValueError):
        status = -1
    reason = parts[2] if len(parts) > 2 else ""
    hdrs = []
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs.append((k.strip(), v.strip()))
    body_buf = bytes(buf[idx + 4:])
    te_chunked = any(
        "chunked" in v.lower()
        for k, v in hdrs if k.lower() == "transfer-encoding")
    cl = None
    for k, v in hdrs:
        if k.lower() == "content-length":
            try:
                cl = int(v)
            except ValueError:
                cl = None
    try:
        if te_chunked:
            body_buf = await _read_chunked(rd, body_buf, timeout)
        elif cl is not None:
            need = min(cl, MAX_BODY)
            while len(body_buf) < need:
                c = await asyncio.wait_for(rd.read(65536), timeout)
                if not c:
                    break
                body_buf += c
            body_buf = body_buf[:min(cl, MAX_BODY)]
        else:
            # eof framing — lê pouco e deixa escapar (body não é o alvo)
            while len(body_buf) < MAX_BODY:
                c = await asyncio.wait_for(rd.read(65536), 0.75)
                if not c:
                    break
                body_buf += c
    except asyncio.TimeoutError:
        pass
    wr.close()
    return (status, reason, hdrs, body_buf[:MAX_BODY])


async def _read_chunked(rd, seed: bytes, timeout: float) -> bytes:
    raw = bytearray(seed)
    out = bytearray()
    for _ in range(256):
        m = raw.find(b"\r\n")
        if m < 0:
            try:
                c = await asyncio.wait_for(rd.read(65536), timeout)
                if not c:
                    break
                raw += c
            except asyncio.TimeoutError:
                break
            continue
        size_tok = raw[:m].split(b";")[0]
        try:
            n = int(size_tok, 16)
        except ValueError:
            break
        if n == 0:
            break
        start = m + 2
        while len(raw) < start + n + 2:
            try:
                c = await asyncio.wait_for(rd.read(65536), timeout)
            except asyncio.TimeoutError:
                return bytes(out)
            if not c:
                return bytes(out)
            raw += c
        if len(raw) >= start + n + 2:
            out += raw[start:start + n]
            raw = raw[start + n + 2:]
        else:
            out += raw[start:]
            raw = b""
            break
    return bytes(out[:MAX_BODY])


def header(hdrs: "list[tuple]", name: str, default=None):
    for k, v in hdrs:
        if k.lower() == name.lower():
            return v
    return default


def cookie_names_from_set_cookie(hdrs: "list[tuple]") -> "list[str]":
    out = []
    for k, v in hdrs:
        if k.lower() == "set-cookie":
            for part in v.split(";"):
                part = part.strip()
                if "=" in part:
                    nm = part.split("=", 1)[0].strip()
                    if nm and nm not in out:
                        out.append(nm)
    return out
