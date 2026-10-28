"""Raw HTTP/1.1 + minimal WebSocket client for C1 tests (stdlib only)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import uuid


class Resp:
    def __init__(self, status, reason, headers, body):
        self.status = status
        self.reason = reason
        self.headers = headers  # list[(name,value)]
        self.body = body

    def header(self, name, default=None):
        for n, v in self.headers:
            if n.lower() == name.lower():
                return v
        return default

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


async def request(
    host: str,
    port: int,
    method: str,
    target: str,
    headers: "dict | None" = None,
    body: bytes = b"",
    timeout: float = 10.0,
) -> Resp:
    hd = f"{method} {target} HTTP/1.1\r\n".encode()
    hdrs = dict(headers or {})
    if "host" not in {k.lower() for k in hdrs}:
        hdrs["host"] = f"{host}:{port}"
    if body and "content-length" not in {k.lower() for k in hdrs}:
        hdrs["content-length"] = str(len(body))
    if method in ("GET", "HEAD", "OPTIONS") and not body:
        pass
    hd += "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()).encode()
    hd += b"\r\n"
    if body:
        hd += body
    rd, wr = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    wr.write(hd)
    await wr.drain()
    # read head
    buf = bytearray()
    while True:
        idx = buf.find(b"\r\n\r\n")
        if idx >= 0:
            break
        chunk = await asyncio.wait_for(rd.read(8192), timeout)
        if not chunk:
            break
        buf.extend(chunk)
    head_lines = buf[: idx + 4][: -(4)].decode("latin-1").split("\r\n") if idx >= 0 else []
    if not head_lines:
        wr.close()
        raise AssertionError("no response head")
    status_line = head_lines[0]
    sp = status_line.split(" ", 1)
    status = int(sp[1].split(" ")[0])
    headers = []
    for ln in head_lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers.append((k, v.strip()))
    body = bytes(buf[idx + 4 :])
    # drain body per framing
    te = sum(1 for n, _ in headers if n.lower() == "transfer-encoding")
    cl_hdr = [v for n, v in headers if n.lower() == "content-length"]
    if 100 <= status < 200 and status != 101:
        pass  # informational: assume none
    elif status == 101:
        pass
    elif status in (204, 304):
        pass
    elif any("chunked" in v.lower() for _, v in ((n, v) for n, v in headers if n.lower() == "transfer-encoding")):
        buf = bytearray(body)
        while True:
            line_end = buf.find(b"\r\n")
            if line_end < 0:
                more = await asyncio.wait_for(rd.read(8192), timeout)
                if not more:
                    break
                buf.extend(more)
                continue
            size = int(buf[:line_end].split(b";")[0], 16)
            if size == 0:
                break
            need = line_end + 2 + size + 2
            while len(buf) < need:
                more = await asyncio.wait_for(rd.read(8192), timeout)
                if not more:
                    break
                buf.extend(more)
            body += bytes(buf[line_end + 2 : line_end + 2 + size])
            del buf[: need]
    elif cl_hdr:
        n = int(cl_hdr[0])
        while len(body) < n:
            more = await asyncio.wait_for(rd.read(8192), timeout)
            if not more:
                break
            body += more
        body = body[:n]
    else:
        while True:
            more = await asyncio.wait_for(rd.read(65536), timeout)
            if not more:
                break
            body += more
    wr.close()
    reason = status_line.split(" ", 2)[2] if len(status_line.split(" ", 2)) == 3 else ""
    return Resp(status, reason, headers, body)


# ------------------------------------------------------------- websocket bits
async def ws_handshake(host: str, port: int, target: str, extra=None):
    key = base64.b64encode(os.urandom(16)).decode()
    hdrs = {
        "host": f"{host}:{port}",
        "upgrade": "websocket",
        "connection": "Upgrade",
        "sec-websocket-key": key,
        "sec-websocket-version": "13",
    }
    hdrs.update(extra or {})
    hd = f"GET {target} HTTP/1.1\r\n".encode()
    hd += "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()).encode()
    hd += b"\r\n"
    rd, wr = await asyncio.open_connection(host, port)
    wr.write(hd)
    await wr.drain()
    buf = bytearray()
    while True:
        if b"\r\n\r\n" in buf:
            break
        chunk = await asyncio.wait_for(rd.read(4096), 10)
        if not chunk:
            break
        buf.extend(chunk)
    head = buf[: buf.index(b"\r\n\r\n") + 4]
    first_frames = bytes(buf[buf.index(b"\r\n\r\n") + 4 :])
    status = int(head.split(b" ", 2)[1].split(b" ")[0])
    return (rd, wr), status, first_frames


def _mask(data: bytes, mask: bytes) -> bytes:
    return bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def ws_frame_text(data: str) -> bytes:
    payload = data.encode()
    # RFC 6455: the masking key is EXACTLY 4 bytes (uuid.bytes is 16 — slicing).
    mask = uuid.uuid4().bytes[:4]
    length = len(payload)
    if length < 126:
        header = bytes([0x81, 0x80 | length])
    elif length < 65536:
        header = bytes([0x81, 0x80 | 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([0x81, 0x80 | 127]) + length.to_bytes(8, "big")
    return header + mask + _mask(payload, mask)


async def ws_read_one_frame(rd: asyncio.StreamReader) -> "str | None":
    b1 = await rd.readexactly(2)
    opcode = b1[0] & 0x0F
    masked = b1[1] & 0x80
    length = b1[1] & 0x7F
    if length == 126:
        length = int.from_bytes((await rd.readexactly(2)), "big")
    elif length == 127:
        length = int.from_bytes((await rd.readexactly(8)), "big")
    mask = (await rd.readexactly(4)) if masked else b""
    payload = await rd.readexactly(length)
    if masked:
        payload = _mask(payload, mask)
    if opcode == 0x8:  # close
        return None
    return payload.decode("utf-8", "replace")
