"""Fake Navidrome upstream for C1 tests — built ON TOP OF OUR OWN http1
parsers (dogfood): a real second implementation would be noise; sharing the
machinery means these tests prove the ROUTING/injection/upgrade policy,
which is what M0' is about.

Routes offered (all on one port, per path):
  GET  /                -> 302 Location: /app/            (relative, like v0.64.2)
  GET  /loc/            -> 302 Location: http://<self>/app/  (ABSOLUTE: rewrite test)
  GET  /app/            -> 200 text/html, CL framing      (plain SPA)
  GET  /app/gz/         -> 200 text/html, gzip + CL
  GET  /app/chunk/      -> 200 text/html, chunked
  GET  /app/chunkgz/    -> 200 text/html, chunked + gzip
  GET  /css/main.css    -> 200 text/css + ACAO:*          (passthrough, no injection)
  GET  /opts/           -> 200 (ACAO/ACAM/ACAH) for OPTIONS too
  POST /login           -> 200 + Set-Cookie: nd_session=SEKRET; Path=/; HttpOnly
  GET  /rest/ping       -> 200 with Cookie, 401 without    (session mechanics)
  GET  /rest/getIndexes -> Subsonic probe endpoint (M1 gate): 200 for a
                             recognized session (u/t, JWT, cookie), 401
                             code-40 otherwise; probe_route_404=True makes
                             it vanish entirely (anchor-break FAIL-CLOSED
                             test)
  GET  /rest/stream     -> 200 audio/wav SILENCE (A6): any request shape —
                             the fake's audio "delivery" is a generated
                             zero-WAV (tiny, in-memory), enough for a media
                             element to load; no real bytes are synthesized.
  GET  /fixtures/<n>.html -> static pages (A6): served from the suite's
                             fixtures/bridge_ui/ directory — the browser-
                             level E2E exercises them THROUGH the proxy
                             (bridge.js is injected into them like into any
                             upstream document).
  GET  /ws              -> RFC6455 echo (prepends "echo:") (cláusula B i)

The document served contains `<head><title>FakeNav</title></head>` so the
injection anchor is realistic.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import os
from urllib.parse import parse_qsl

from navidrome_remote.http1 import read_head, read_request_body

SESSION_COOKIE_VALUE = "SEKRET"
DOC_PLAIN = (
    "<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
    "<title>FakeNav</title></head><body><div id=app></div></body></html>"
).encode()

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ---------------------------------------------------------------- A6
# Zero-filled WAV (8 kHz, 16-bit mono, 0.5 s): the "audio" this fake
# hands out for /rest/stream. Generated once at import time — it is a
# stand-in for real audio bytes (C1: no audio validation), and it makes
# a media element progress normally (loadeddata/playing) without any
# external content.
def _make_silence_wav(seconds: float = 0.5, sr: int = 8000) -> bytes:
    import struct

    n = int(sr * seconds)
    fmt = struct.pack("<IIHHII", 16, 1, sr, sr * 2, 2, 16)
    data = b"\x00\x00" * n
    return (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + fmt
        + b"data" + struct.pack("<I", len(data)) + data
    )


SILENCE_WAV = _make_silence_wav()

# A6 fixture pages (bridge_UI) live next to the E2E suite.
_FIXTURE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "suite", "fixtures", "bridge_ui")
)


class FakeUpstream:
    def __init__(self, subsonic_users=None, subsonic_jwts=None,
                 cookie_sessions=None, probe_route_404=False):
        self.host = "127.0.0.1"
        self.port = 0
        self.authority = ""
        self._server = None
        self.requests = []  # (method, path, {headers}) — for assertion
        # M1: credential tables for the /rest/getIndexes probe endpoint.
        # subsonic_users: user -> expected t (md5(password+salt))
        # subsonic_jwts:  set of JWT strings the upstream recognizes
        # cookie_sessions: cookie VALUE -> identity (opaque forward channel)
        self.subsonic_users = subsonic_users or {}
        self.subsonic_jwts = set(subsonic_jwts or ())
        self.cookie_sessions = cookie_sessions or {}
        self.probe_route_404 = probe_route_404

    async def start(self):
        self._server = await asyncio.start_server(self._on, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self.authority = f"{self.host}:{self.port}"
        return self

    async def aclose(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ---------------------------------------------------------------- client
    async def _on(self, reader, writer):
        try:
            while True:
                rq, leftover = await read_head(reader, "request")
                if rq is None:
                    break
                body = b""
                if rq.method in ("POST", "PUT", "PATCH"):
                    body = await read_request_body(reader, leftover, rq.headers, 1 << 20)
                self.requests.append((rq.method, rq.target, rq.headers))
                resp = self._handle(rq, body)
                if resp is None:
                    break
                writer.write(resp)
                await writer.drain()
                p = rq.target.split("?")[0]
                if (rq.headers.get_first("upgrade") or "").lower() == "websocket":
                    # Protocol switched: the HTTP loop ends here. On the echo
                    # route we then speak raw RFC6455 frames (cláusula B(i)
                    # green witness); the refusal route ends as plain HTTP.
                    if p == "/ws":
                        # `leftover` may already hold the first frame bytes
                        # (TCP coalesces the handshake + first frame) — seed.
                        await self._ws_echo_loop(reader, writer, leftover)
                    break
                if (rq.headers.get_first("connection") or "").lower().find("close") >= 0:
                    break
        except Exception:
            pass  # a torn-down fixture connection must not kill the loop
        finally:
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass

    def _handle(self, rq, body):
        p = rq.target.split("?")[0]
        if rq.method == "GET" and p == "/":
            return self._reply(
                302,
                [("location", "/app/"), ("content-type", "text/plain")],
                b"",
            )
        if rq.method == "GET" and p == "/loc/":
            return self._reply(
                302,
                [("location", f"http://{self.authority}/app/"), ("content-type", "text/plain")],
                b"",
            )
        if rq.method == "GET" and p == "/app/":
            return self._cl(200, "text/html; charset=utf-8", DOC_PLAIN)
        if rq.method == "GET" and p == "/app/gz/":
            return self._cl(
                200,
                "text/html; charset=utf-8",
                gzip.compress(DOC_PLAIN),
                extra=[("content-encoding", "gzip")],
            )
        if rq.method == "GET" and p == "/app/chunk/":
            return self._chunked(200, "text/html; charset=utf-8", DOC_PLAIN)
        if rq.method == "GET" and p == "/app/chunkgz/":
            return self._chunked(
                200, "text/html; charset=utf-8", gzip.compress(DOC_PLAIN),
                extra=[("content-encoding", "gzip")],
            )
        if rq.method == "GET" and p == "/css/main.css":
            return self._cl(
                200,
                "text/css",
                b"body{margin:0}",
                extra=[("access-control-allow-origin", "*")],
            )
        if p == "/opts/" and rq.method in ("OPTIONS", "GET"):
            if rq.method == "OPTIONS":
                return self._cl(
                    200,
                    "text/plain",
                    b"",
                    extra=[
                        ("access-control-allow-origin", "*"),
                        ("access-control-allow-methods", "GET"),
                        ("access-control-allow-headers", "range, cookie"),
                    ],
                )
            return self._cl(200, "text/plain", b"ok")
        if rq.method == "POST" and p == "/login":
            return self._cl(
                200,
                "text/plain",
                b"logged-in(fake)",
                extra=[
                    (
                        "set-cookie",
                        f"nd_session={SESSION_COOKIE_VALUE}; Path=/; HttpOnly",
                    )
                ],
            )
        if rq.method == "GET" and p == "/rest/getIndexes":
            return self._handle_get_indexes(rq)
        if rq.method == "GET" and p == "/rest/ping":
            ck = rq.headers.get_first("cookie") or ""
            if f"nd_session={SESSION_COOKIE_VALUE}" in ck:
                return self._cl(200, "text/plain", b"pong")
            return self._reply(
                401,
                [
                    ("www-authenticate", 'Basic realm="navidrome"'),
                    ("content-type", "text/plain"),
                ],
                b"auth-required",
            )
        if rq.method == "GET" and p == "/rest/stream":
            # A6: the "audio delivery" of the fake. One small silence
            # file answers every shape (with or without Range — the
            # whole 8-KB payload fits a single datagram-sized read, so
            # a 200 full-body answer satisfies ranged clients too).
            return self._cl(
                200,
                "audio/wav",
                SILENCE_WAV,
                extra=[
                    ("access-control-allow-origin", "*"),
                    ("accept-ranges", "bytes"),
                ],
            )
        if rq.method == "GET" and p.startswith("/fixtures/"):
            name = p[len("/fixtures/"):]
            safe = ("/" not in name) and (name.endswith(".html"))
            fp = os.path.join(_FIXTURE_DIR, name) if safe else ""
            if fp and os.path.isfile(fp):
                with open(fp, "rb") as fh:
                    return self._cl(
                        200,
                        "text/html; charset=utf-8",
                        fh.read(),
                        extra=[("access-control-allow-origin", "*")],
                    )
            return self._reply(
                404, [("content-type", "text/plain")], b"fixture-not-found")
        if rq.method == "GET" and p == "/ws":
            key = rq.headers.get_first("sec-websocket-key") or ""
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            # 101 is a SWITCHING RESPONSE: it terminates with the blank line
            # (no body) — the proxy's read_head relies on that delimiter.
            head = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "upgrade: websocket\r\n"
                "connection: Upgrade\r\n"
                f"sec-websocket-accept: {accept}\r\n"
                "\r\n"
            )
            return head.encode()
        if rq.method == "GET" and p == "/ws-red":
            return self._reply(
                426,
                [("upgrade", "none"), ("content-type", "text/plain")],
                b"upgrade-refused",
            )
        return self._reply(404, [("content-type", "text/plain")], b"not-found-fake")

    # ------------------------------------------------------------ getIndexes
    def _handle_get_indexes(self, rq):
        """Subsonic liveness probe used by the M1 session gate.

        Recognition mirrors the v0.64.2 contract observed in A2:
        u/t/s query params, a JWT (query or Authorization header), or an
        opaque cookie. Valid -> 200 {"success": true}; invalid -> 401 with
        Subsonic code 40; probe_route_404 -> the route disappears (404),
        simulating the anchor break."""
        if self.probe_route_404:
            return self._reply(
                404,
                [("content-type", "application/json")],
                b"{}")
        qs = rq.target.split("?", 1)[1] if "?" in rq.target else ""
        params = dict(parse_qsl(qs))
        ok = False
        jwt = params.get("jwt")
        if not jwt:
            auth = rq.headers.get_first("authorization") or ""
            if auth.lower().startswith("bearer "):
                jwt = auth[len("bearer "):].strip()
        if jwt:
            ok = jwt in self.subsonic_jwts
        elif "u" in params:
            ok = self.subsonic_users.get(params["u"]) == params.get("t", "")
        else:
            ck = rq.headers.get_first("cookie") or ""
            for frag in ck.split(";"):
                frag = frag.strip()
                if "=" in frag:
                    _, v = frag.split("=", 1)
                    if v in self.cookie_sessions:
                        ok = True
            if not ok and f"nd_session={SESSION_COOKIE_VALUE}" in ck:
                ok = True
        if ok:
            return self._reply(
                200,
                [("content-type", "application/json")],
                json.dumps({"success": True, "indexes": []}).encode())
        return self._reply(
            401,
            [("content-type", "application/json")],
            json.dumps(
                {"code": 40,
                 "fields": {"u": "Invalid username/password"},
                 "errors": {"u": "Invalid credentials"}}
            ).encode())

    # ------------------------------------------------------------------ ws echo
    async def _ws_echo_loop(self, reader, writer, seed: bytes = b"") -> None:
        """Minimal RFC6455 echo: unmask incoming frames, reply with an
        unmasked text frame prefixed with 'echo:' (green B(i) behaviour)."""
        buf = bytearray(seed)
        while True:
            opcode, payload = await self._read_frame(reader, buf)
            if opcode == 0x8:  # close frame — mirror by ending the connection
                break
            if opcode not in (0x1, 0x2):  # ping/pong/control: ignore in the mock
                continue
            out = ("echo:" + payload.decode("utf-8", "replace")).encode()
            writer.write(self._frame_text_unmasked(out))
            await writer.drain()

    async def _read_frame(self, reader, buf: bytearray) -> "tuple[int, bytes]":
        async def need(n: int) -> bytes:
            while len(buf) < n:
                chunk = await reader.read(65536)
                if not chunk:
                    raise ConnectionError("eof dentro de um frame ws")
                buf.extend(chunk)
            out = bytes(buf[:n])
            del buf[:n]
            return out

        b1 = await need(2)
        opcode = b1[0] & 0x0F
        masked = b1[1] & 0x80
        length = b1[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await need(2), "big")
        elif length == 127:
            length = int.from_bytes(await need(8), "big")
        mask = await need(4) if masked else b""
        payload = await need(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    @staticmethod
    def _frame_text_unmasked(data: bytes) -> bytes:
        if len(data) < 126:
            header = bytes([0x81, len(data)])
        elif len(data) < 65536:
            header = bytes([0x81, 126]) + len(data).to_bytes(2, "big")
        else:
            header = bytes([0x81, 127]) + len(data).to_bytes(8, "big")
        return header + data

    # -------------------------------------------------------------- builders
    def _reply(self, status, headers, body):
        reasons = {
            200: "OK",
            302: "Found",
            401: "Unauthorized",
            404: "Not Found",
            426: "Upgrade Required",
        }
        hdrs = "".join(f"{n}: {v}\r\n" for n, v in headers)
        return (
            f"HTTP/1.1 {status} {reasons.get(status, 'X')}\r\n"
            f"{hdrs}"
            f"content-length: {len(body)}\r\n"
            f"connection: close\r\n"
            f"\r\n"
        ).encode() + body

    def _cl(self, status, ctype, body, extra=None):
        return self._reply(
            status,
            [("content-type", ctype)] + list(extra or []),
            body,
        )

    def _chunked(self, status, ctype, body, extra=None):
        parts = bytearray()
        off = 0
        while off < len(body):
            piece = body[off : off + 7]
            parts += f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
            off += len(piece)
        parts += b"0\r\n\r\n"
        hdrs = "".join(f"{n}: {v}\r\n" for n, v in [("content-type", ctype)] + list(extra or []))
        return (
            f"HTTP/1.1 {status} OK\r\n"
            f"{hdrs}"
            f"transfer-encoding: chunked\r\n"
            f"connection: close\r\n"
            f"\r\n"
        ).encode() + bytes(parts)
