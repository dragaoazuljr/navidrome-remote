"""Small HTTP/1.1 machinery for a transparent proxy (stdlib only).

Scope discipline: this module never *decides* semantics — it splits wire
bytes into (head, body) and reassembles them. Policy (what gets transformed)
lives in proxy.py / inject.py. Everything forwarded is reproduced byte-wise.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import zlib
from dataclasses import dataclass, field

MAX_HEAD_BYTES = 32 * 1024


class WireError(Exception):
    """Malformed client traffic or unexpected upstream framing.

    Carries an HTTP status so the proxy can answer 4xx/5xx to the client
    instead of dropping the connection without a word.
    """

    def __init__(self, msg: str, status: int = 400, body: str = ""):
        super().__init__(msg)
        self.msg = msg
        self.status = status
        self.body = body or msg


@dataclass
class HeadList:
    """Ordered (name, value) pairs, cases preserved, duplicates tolerated."""

    pairs: list = field(default_factory=list)

    def add(self, name: str, value: str) -> None:
        self.pairs.append((name, value))

    def remove(self, name: str) -> None:
        key = name.lower()
        self.pairs = [(n, v) for n, v in self.pairs if n.lower() != key]

    def replace(self, name: str, value: str) -> None:
        key = name.lower()
        found = False
        for i, (n, _) in enumerate(self.pairs):
            if n.lower() == key:
                self.pairs[i] = (n, value)
                found = True
        if not found:
            self.add(name, value)

    def get(self, name: str) -> "list[str]":
        key = name.lower()
        return [v for n, v in self.pairs if n.lower() == key]

    def get_first(self, name: str) -> "str | None":
        vals = self.get(name)
        return vals[0] if vals else None

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def encode(self) -> bytes:
        return "".join(f"{n}: {v}\r\n" for n, v in self.pairs).encode("latin-1")

    @staticmethod
    def parse(lines) -> "HeadList":
        hl = HeadList()
        for ln in lines:
            if not ln:
                continue
            if ":" not in ln:
                raise WireError("header malformado")
            k, v = ln.split(":", 1)
            hl.add(k, v.strip())
        return hl


@dataclass
class RequestHead:
    method: str
    target: str
    version: str
    headers: HeadList
    raw: bytes  # request line + headers as received (for verbatim resend)


@dataclass
class ResponseHead:
    status_line: str
    version: str
    status: int
    reason: str
    headers: HeadList
    raw: bytes  # status line + headers as received


def _split_request(reqline: str) -> RequestHead:
    parts = reqline.split(" ", 2)
    if len(parts) != 3:
        raise WireError("request-line malformada", 400)
    method, target, version = parts
    if not version.startswith("HTTP/"):
        raise WireError("versão HTTP inválida", 505)
    return RequestHead(method.upper(), target, version, HeadList(), raw=b"")


def _split_response(status_line: str) -> ResponseHead:
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise WireError("status-line malformada", 502)
    try:
        status = int(parts[1])
    except ValueError:
        raise WireError("status não numérico", 502)
    reason = parts[2] if len(parts) == 3 else ""
    return ResponseHead(status_line, parts[0], status, reason, HeadList(), raw=b"")


async def read_head(
    reader: asyncio.StreamReader,
    kind: str,
    cap: int = MAX_HEAD_BYTES,
    seed: bytes = b"",
) -> "tuple[RequestHead | ResponseHead, bytes] | None":
    """Read one complete head from `reader`.

    `seed` pre-fills the accumulation buffer with stray bytes already in
    hand (leftover from a previous transaction). Returns (parsed, leftover)
    where leftover is anything after the blank line (body bytes already on
    the wire). Returns None on clean EOF before a complete head (client
    idle-closed a keepalive connection).
    """
    buf = bytearray(seed)
    while True:
        idx = buf.find(b"\r\n\r\n")
        if idx >= 0:
            head_bytes = bytes(buf[: idx + 4])
            leftover = bytes(buf[idx + 4 :])
            lines = head_bytes[:-4].decode("latin-1").split("\r\n")
            if kind == "request":
                rq = _split_request(lines[0])
                rq.headers = HeadList.parse(lines[1:])
                rq.raw = head_bytes[:-4]
                return rq, leftover
            rp = _split_response(lines[0])
            rp.headers = HeadList.parse(lines[1:])
            rp.raw = head_bytes[:-4]
            return rp, leftover
        if len(buf) > cap:
            raise WireError("head muito grande", 431)
        chunk = await reader.read(8192)
        if not chunk:
            if not buf:
                return None
            raise WireError("fim de conexão no meio do head", 400)
        buf.extend(chunk)


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------

def int_or_zero(s: "str | None") -> int:
    try:
        v = int("" if s is None else s)
        return v if v >= 0 else 0
    except ValueError:
        return 0


def _clamp(n: int, cap: int, status: int = 413) -> int:
    if n > cap:
        raise WireError("conteúdo excede o teto configurado", status)
    return n


async def read_exact(reader: asyncio.StreamReader, n: int, cap: int) -> bytes:
    _clamp(n, cap)
    buf = bytearray()
    while len(buf) < n:
        chunk = await reader.read(min(65536, n - len(buf)))
        if not chunk:
            raise WireError("fim de conexão no meio do corpo", 400)
        buf.extend(chunk)
    return bytes(buf)


@dataclass
class BodyStream:
    """Buffered reader over an asyncio stream, seeded with stray left bytes."""

    reader: asyncio.StreamReader
    _buffer: bytes = b""
    _budget: int = 0

    def __post_init__(self):
        pass

    @classmethod
    def make(cls, reader: asyncio.StreamReader, seed: bytes, budget: int) -> "BodyStream":
        bs = cls.__new__(cls)
        bs.reader = reader
        bs._buffer = seed
        bs._budget = budget
        return bs

    async def readline(self, delim: bytes = b"\r\n", cap_line: int = 8192) -> bytes:
        while True:
            idx = self._buffer.find(delim)
            if idx >= 0:
                line = self._buffer[: idx + len(delim)]
                self._buffer = self._buffer[idx + len(delim) :]
                return line
            if len(self._buffer) > cap_line:
                raise WireError("linha excessivamente longa", 400)
            chunk = await self.reader.read(8192)
            if not chunk:
                rem, self._buffer = self._buffer, b""
                return rem
            self._buffer += chunk

    async def read_exact(self, n: int) -> bytes:
        _clamp(n, self._budget, 400)
        self._budget -= n
        while len(self._buffer) < n:
            chunk = await self.reader.read(min(65536, n))
            if not chunk:
                raise WireError("fim de conexão no meio do corpo", 400)
            self._buffer += chunk
        out = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return out


def _parse_chunk_size(line: str) -> int:
    size_str = line.split(";")[0].strip()
    try:
        return int(size_str, 16)
    except ValueError:
        raise WireError("tamanho de chunk inválido", 400)


async def _decode_chunks(stream: BodyStream) -> bytes:
    """Decode a chunked body to raw bytes (framing discarded, trailers eaten)."""
    out = bytearray()
    while True:
        line = await stream.readline()
        if not line:
            raise WireError("fim de conexão em chunk", 400)
        size = _parse_chunk_size(line.rstrip(b"\r\n").decode("latin-1"))
        if size == 0:
            while True:
                tl = await stream.readline()
                if not tl or tl.rstrip(b"\r\n") == b"":
                    break
            break
        out.extend(await stream.read_exact(size))
        await stream.read_exact(2)  # chunk terminator CRLF
    return bytes(out)


async def read_request_body(
    reader: asyncio.StreamReader,
    leftover: bytes,
    headers: HeadList,
    cap: int,
) -> bytes:
    """Materialize the client request body (chunked or content-length)."""
    te = (headers.get_first("transfer-encoding") or "").lower()
    if "chunked" in te:
        return await _decode_chunks(BodyStream.make(reader, leftover, cap))
    cl = headers.get_first("content-length")
    if cl is not None:
        n = _clamp(int_or_zero(cl), cap)
        if n == 0:
            return b""
        if leftover:
            if len(leftover) >= n:
                return leftover[:n]
            return leftover + await read_exact(reader, n - len(leftover), cap - len(leftover))
        return await read_exact(reader, n, cap)
    if leftover:
        _clamp(len(leftover), cap)
        return leftover
    return b""


async def read_response_body_raw(
    reader: asyncio.StreamReader,
    leftover: bytes,
    headers: HeadList,
    status: int,
    cap: int,
) -> "tuple[bytes, str]":
    """Buffer an upstream response body and report its framing.

    Returns (body_bytes, framing):
      'none'  — 1xx/204/304: no body
      'cl:N'  — content-length framed (re-emit with computed CL)
      'te'    — chunked (body decoded; re-emit chunked unless transformed)
      'eof'   — unframed: upstream closed to end body
    """
    if status in (204, 304) or 100 <= status < 200:
        return b"", "none"
    te = (headers.get_first("transfer-encoding") or "").lower()
    if "chunked" in te:
        return await _decode_chunks(BodyStream.make(reader, leftover, cap)), "te"
    cl = headers.get_first("content-length")
    if cl is not None:
        n = _clamp(int_or_zero(cl), cap, 502)
        if n == 0:
            return b"", "cl:0"
        if leftover:
            if len(leftover) >= n:
                return leftover[:n], f"cl:{n}"
            return (leftover + await read_exact(reader, n - len(leftover), cap - len(leftover))), f"cl:{n}"
        return await read_exact(reader, n, cap), f"cl:{n}"
    # Unframed: read until the upstream closes the connection.
    out = bytearray(leftover)
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        out.extend(chunk)
        _clamp(len(out), cap, 502)
    return bytes(out), "eof"


# ---------------------------------------------------------------------------
# Chunked re-emission
# ---------------------------------------------------------------------------

def chunk_encode(body: bytes) -> bytes:
    if not body:
        return b"0\r\n\r\n"
    out = bytearray()
    offset = 0
    while offset < len(body):
        piece = body[offset : offset + 65536]
        out += f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
        offset += len(piece)
    out += b"0\r\n\r\n"
    return bytes(out)


# ---------------------------------------------------------------------------
# Content-Encoding
# ---------------------------------------------------------------------------

def detect_ce(header_value: "str | None") -> "str | None":
    if not header_value:
        return None
    v = header_value.strip().lower()
    if v.startswith("gzip"):
        return "gzip"
    if v.startswith("deflate"):
        return "deflate"
    if v in ("", "identity"):
        return None
    return v  # br etc. — opaque: never decoded, never transformed


def decode_body(body: bytes, ce: "str | None") -> "tuple[bytes, str | None]":
    """(plain_bytes, ce_used). Unknown encings pass through untouched."""
    if ce == "gzip":
        return zlib.decompress(body, 16 + zlib.MAX_WBITS), "gzip"
    if ce == "deflate":
        last_err: "Exception | None" = None
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(body, wbits), "deflate"
            except zlib.error as exc:
                last_err = exc
        raise WireError("corpus deflate ilegível", 502) from last_err
    return body, ce


def gzip_compress(body: bytes) -> bytes:
    return gzip.compress(body)
