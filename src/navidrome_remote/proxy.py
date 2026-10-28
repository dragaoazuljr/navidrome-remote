"""Transparent reverse proxy — the heart of the single service.

Routing table (one process, one listen port):

    GET /bridge.js            -> local static asset (ours)
    *   /api/v1/*             -> local JSON API (drives the playback engine)
    everything else            -> upstream Navidrome (byte-transparent)
                                     ├─ GET text/html      -> bridge.js injected
                                     ├─ Upgrade: websocket -> bidirectional
                                     │  splice (cláusula B(i))
                                     └─ cookies/etc.       -> verbatim
                                     (cláusula B(ii): Set-Cookie untouched)

Transparency budget: the HTTP layer is TERMINATED for (a) our own routes,
(b) transformed HTML documents, and (c) 101 handshakes. For everything else
(SPA boot stream, REST, streams) we reproduce the exchange byte-for-byte
modulo RFC 7230 hop-by-hop headers and an absolute-`Location` rewrite aimed
back at the client's own origin.

Keepalive model: the CLIENT connection persists across transactions (the
browser pools it). Every transaction opens its own upstream connection
(Connection: close), so there is no cross-talk between pooled upstream
streams — correctness first on a 1 GB box; the LAN absorbs the extra
handshakes comfortably.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import zlib
from urllib.parse import urlsplit

from .config import Config
from .http1 import (
    MAX_HEAD_BYTES,
    WireError,
    chunk_encode,
    decode_body,
    detect_ce,
    gzip_compress,
    read_head,
    read_request_body,
    read_response_body_raw,
)
from .inject import inject_bridge
from .spy import Spy

# Hop-by-hop headers (RFC 7230 §6.1) — never forwarded across connections.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "upgrade",
        "te",
        "trailers",
    }
)


class ProxyServer:
    def __init__(self, cfg: Config, spy: Spy, api, bridge_asset: bytes):
        self.cfg = cfg
        self.spy = spy
        self.api = api
        self.bridge_asset = bridge_asset
        self.bridge_etag = '"' + hashlib.sha1(bridge_asset).hexdigest() + '"'
        self._server = None

    # ------------------------------------------------------------------ start
    async def start(self):
        self._server = await asyncio.start_server(
            self._on_client, self.cfg.listen_addr, self.cfg.listen_port
        )
        return self._server

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ================================================================ client
    async def _on_client(self, reader, writer) -> None:
        peer = writer.get_extra_info("peername")
        cid = f"{peer[0]}:{peer[1]}" if peer else "opaque"
        leftover = b""
        try:
            while True:
                t0 = time.monotonic()
                try:
                    res = await asyncio.wait_for(
                        read_head(reader, "request", seed=leftover),
                        timeout=max(1.0, self.cfg.keepalive_idle),
                    )
                except asyncio.TimeoutError:
                    await self.spy.event(
                        "err",
                        cid=cid,
                        stage="keepalive-idle",
                        msg=f"idle > {self.cfg.keepalive_idle}s",
                    )
                    break
                if res is None:
                    break  # clean client EOF on a keepalive connection
                rq, leftover = res
                host_seen = rq.headers.get_first("host") or self.cfg.upstream_authority
                is_upgrade = any(
                    u.lower() == "websocket" for u in rq.headers.get("upgrade")
                )
                await self.spy.event(
                    "req",
                    cid=cid,
                    method=rq.method,
                    path=rq.target,
                    host=host_seen,
                    upgrade=is_upgrade,
                    cookies=Spy.cookie_names(rq.headers.get_first("cookie")),
                )
                close_after = False
                try:
                    if rq.target.startswith("/bridge.js"):
                        close_after = await self._serve_bridge(rq, writer)
                    elif rq.target.startswith("/api/v1/"):
                        close_after = await self._serve_api(rq, reader, writer, leftover)
                    elif is_upgrade:
                        close_after = await self._forward_upgrade(
                            rq, reader, writer, cid, t0
                        )
                    else:
                        close_after = await self._forward(
                            rq, reader, writer, cid, t0, leftover
                        )
                except WireError as exc:
                    await self._synthetic_json(
                        writer, exc.status, {"error": "protocol", "detail": exc.msg}
                    )
                    await self.spy.event(
                        "err",
                        cid=cid,
                        stage="client-protocol",
                        msg=exc.msg,
                        status=exc.status,
                    )
                    close_after = True
                if close_after:
                    break
                leftover = b""
        except (ConnectionResetError, BrokenPipeError) as exc:
            await self.spy.event("err", cid=cid, stage="client-gone", msg=str(exc))
        except (OSError, asyncio.TimeoutError) as exc:
            await self.spy.event(
                "err",
                cid=cid,
                stage="client-io",
                msg=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                await writer.drain()
            except (OSError, ConnectionResetError):
                pass
            writer.close()

    # ----------------------------------------------------------- local routes
    async def _serve_bridge(self, rq, writer) -> bool:
        inm = rq.headers.get_first("if-none-match")
        if inm and self.bridge_etag in inm:
            await self._synthesize(
                writer,
                304,
                "Not Modified",
                [("etag", self.bridge_etag), ("cache-control", "no-cache")],
                b"",
                conn_close=True,
            )
            return True
        body = b"" if rq.method == "HEAD" else self.bridge_asset
        headers = [
            ("content-type", "application/javascript; charset=utf-8"),
            ("cache-control", "no-cache"),
            ("etag", self.bridge_etag),
            ("access-control-allow-origin", "*"),
        ]
        await self._synthesize(writer, 200, "OK", headers, body, conn_close=True)
        await self.spy.event(
            "res",
            path="/bridge.js",
            status=200,
            local="bridge",
            bytes_=len(self.bridge_asset),
        )
        return True

    async def _serve_api(self, rq, reader, writer, leftover: bytes = b"") -> bool:
        # NOTE: `leftover` carries request-body bytes already pulled off the
        # wire by read_head; dropping it deadlocks (the client sent everything
        # and waits for the reply while we wait for bytes that never come).
        if rq.method == "OPTIONS":
            # Preflight for the LOCAL control plane: never proxied to the
            # upstream (A2 map: /api/v1/* cells are BY-DESIGN asymmetric).
            # The session gate decides afterwards — a valid preflight does
            # NOT imply an authorized call.
            await self._synthesize(
                writer,
                204,
                "No Content",
                [
                    ("access-control-allow-origin", "*"),
                    ("access-control-allow-methods", "GET, POST"),
                    ("access-control-allow-headers", "authorization, content-type"),
                    ("vary", "Origin"),
                    ("cache-control", "no-store"),
                ],
                b"",
                conn_close=True,
            )
            await self.spy.event("res", path=rq.target, status=204, local="api-options")
            return True
        body = b""
        if rq.method in ("POST", "PUT", "PATCH"):
            body = await read_request_body(
                reader, leftover, rq.headers, self.cfg.max_request_bytes
            )
        status, ctype, payload = await self.api.handle(
            rq.method, rq.target, rq.headers, body
        )
        await self._synthesize(
            writer,
            status,
            "",
            [("content-type", ctype), ("cache-control", "no-store")],
            payload,
            conn_close=True,
        )
        await self.spy.event("res", path=rq.target, status=status, local="api")
        return True

    # ================================================================== proxy
    async def _forward(self, rq, reader, writer, cid, t0, leftover: bytes = b"") -> bool:
        req_body = await read_request_body(
            reader, leftover, rq.headers, self.cfg.max_request_bytes
        )
        up = await self._open_upstream(cid)
        if up is None:
            await self._synthetic_json(writer, 502, {"error": "upstream-unreachable"})
            return True
        up_rd, up_wr = up
        try:
            close_after = await self._forward_tx(
                rq, req_body, up_rd, up_wr, reader, writer, cid, t0
            )
        except WireError as exc:
            await self._synthetic_json(
                writer, 502, {"error": "upstream-protocol", "detail": exc.msg}
            )
            await self.spy.event(
                "err", cid=cid, stage="upstream-protocol", msg=exc.msg
            )
            close_after = True
        except (OSError, asyncio.TimeoutError) as exc:
            await self._synthetic_json(writer, 502, {"error": "upstream-connection-lost"})
            await self.spy.event(
                "err",
                cid=cid,
                stage="upstream-io",
                msg=f"{type(exc).__name__}: {exc}",
            )
            close_after = True
        finally:
            await self._close_upstream(up)
        return close_after

    async def _forward_tx(
        self, rq, req_body, up_rd, up_wr, reader, writer, cid, t0
    ) -> bool:
        host_seen = rq.headers.get_first("host") or self.cfg.upstream_authority

        # ------------ outgoing request (verbatim modulo policy)
        out_pairs = list(rq.headers.pairs)
        had_te = any(n.lower() == "transfer-encoding" for n, _ in rq.headers.pairs)
        out_pairs = [
            (n, v)
            for n, v in out_pairs
            if n.lower() not in ("expect", "transfer-encoding", "connection")
        ]
        if had_te:
            out_pairs.append(("content-length", str(len(req_body))))
        replaced = []
        host_found = False
        for n, v in out_pairs:
            if n.lower() == "host":
                replaced.append(("host", self.cfg.vhost_authority))
                host_found = True
            else:
                replaced.append((n, v))
        if not host_found:
            replaced.insert(0, ("host", self.cfg.vhost_authority))
        replaced.append(("connection", "close"))
        hd_wire = (
            f"{rq.method} {rq.target} {rq.version}\r\n".encode("latin-1")
            + b"".join(f"{n}: {v}\r\n".encode("latin-1") for n, v in replaced)
            + b"\r\n"
        )
        up_wr.write(hd_wire)
        if req_body:
            up_wr.write(req_body)
        await up_wr.drain()

        # ------------ response head (past informational 1xx; 101 splices)
        while True:
            resp, leftover = await read_head(up_rd, "response", MAX_HEAD_BYTES)
            if resp is None:
                raise WireError("upstream fechou sem resposta", 502)
            if 100 <= resp.status < 200:
                if resp.status == 101:
                    await self._splice_101(resp, leftover, rq, up_rd, up_wr, reader, writer, cid)
                    return True
                writer.write(resp.raw + b"\r\n\r\n")
                await writer.drain()
                continue
            break

        head_only = rq.method == "HEAD"
        if head_only:
            body, framing = b"", "none"
        else:
            body, framing = await read_response_body_raw(
                up_rd, leftover, resp.headers, resp.status, self.cfg.max_html_bytes
            )

        ct = (resp.headers.get_first("content-type") or "").lower().strip()
        transformed = False
        out_pairs = [
            (n, v) for n, v in resp.headers.pairs if n.lower() not in _HOP_BY_HOP
        ]

        if (
            rq.method == "GET"
            and resp.status == 200
            and body
            and ct.startswith("text/html")
        ):
            ce = detect_ce(resp.headers.get_first("content-encoding"))
            if ce in (None, "gzip", "deflate"):
                plain, _used = decode_body(body, ce)
                new_html, ok = inject_bridge(plain)
                if ok:
                    # Transparency: keep the upstream's encoding. A plain
                    # document goes back plain (a client that asked for
                    # identity must never receive gzip); a gzip document
                    # stays gzip (bandwidth on the 1 GB box).
                    if ce == "gzip":
                        out_body = gzip_compress(new_html)
                    elif ce == "deflate":
                        out_body = zlib.compress(new_html)
                    else:
                        out_body = new_html
                    out_pairs = [
                        (n, v)
                        for n, v in out_pairs
                        if n.lower()
                        not in ("content-encoding", "content-length", "transfer-encoding")
                    ]
                    if ce is not None:
                        out_pairs.append(("content-encoding", ce))
                    out_pairs.append(("content-length", str(len(out_body))))
                    body = out_body
                    framing = "cl"
                    transformed = True
                else:
                    await self.spy.event(
                        "transform_skip",
                        path=rq.target,
                        reason="ja-presente-ou-sem-ancora",
                    )
            else:
                await self.spy.event(
                    "transform_skip",
                    path=rq.target,
                    reason=f"encoding-opaco-{ce or '-'}",
                )

        if not transformed:
            if framing == "te":
                out_pairs = [
                    (n, v) for n, v in out_pairs if n.lower() != "transfer-encoding"
                ]
                out_pairs.append(("transfer-encoding", "chunked"))
                body = chunk_encode(body)
            elif framing == "eof":
                out_pairs.append(("connection", "close"))
            elif framing == "none":
                out_pairs = [
                    (n, v) for n, v in out_pairs if n.lower() != "content-length"
                ]

        # ------------ Location rewrite (absolute upstream -> client origin)
        if 300 <= resp.status < 400:
            new_pairs = []
            for n, v in out_pairs:
                if n.lower() == "location":
                    nw = self._rewrite_location(v, host_seen)
                    if nw != v:
                        v = nw
                new_pairs.append((n, v))
            out_pairs = new_pairs

        wire = (
            resp.status_line.encode("latin-1")
            + b"\r\n"
            + b"".join(f"{n}: {v}\r\n".encode("latin-1") for n, v in out_pairs)
            + b"\r\n"
            + body
        )
        writer.write(wire)
        await writer.drain()

        client_wants_close = "close" in (rq.headers.get_first("connection") or "").lower()
        close_after = framing == "eof" or client_wants_close

        sc_hdr = resp.headers.get_first("set-cookie")
        await self.spy.event(
            "res",
            cid=cid,
            path=rq.target,
            status=resp.status,
            content_type=(resp.headers.get_first("content-type") or "")[:64],
            ce=(resp.headers.get_first("content-encoding") or "")[:16],
            set_cookie=[
                k.strip().split("=", 1)[0] for k in sc_hdr.split(";") if k.strip()
            ]
            if sc_hdr
            else [],
            bytes_=len(body),
            transformed=transformed,
            ms=int((time.monotonic() - t0) * 1000),
        )
        return close_after

    # ------------------------------------------------------------ ws upgrade
    async def _forward_upgrade(self, rq, reader, writer, cid, t0) -> bool:
        up = await self._open_upstream(cid)
        if up is None:
            await self._synthetic_json(writer, 502, {"error": "upstream-unreachable"})
            return True
        up_rd, up_wr = up
        try:
            out_pairs = [(n, v) for n, v in rq.headers.pairs if n.lower() != "expect"]
            replaced = []
            host_found = False
            for n, v in out_pairs:
                if n.lower() == "host":
                    replaced.append(("host", self.cfg.vhost_authority))
                    host_found = True
                else:
                    replaced.append((n, v))
            if not host_found:
                replaced.insert(0, ("host", self.cfg.vhost_authority))
            hd_wire = (
                f"{rq.method} {rq.target} {rq.version}\r\n".encode("latin-1")
                + b"".join(f"{n}: {v}\r\n".encode("latin-1") for n, v in replaced)
                + b"\r\n"
            )
            up_wr.write(hd_wire)
            await up_wr.drain()

            resp, leftover = await read_head(up_rd, "response", MAX_HEAD_BYTES)
            if resp is None:
                raise WireError("upstream fechou no handshake", 502)
            if resp.status != 101:
                # RED (cláusula B i): upstream refuses the upgrade.
                await self.spy.event(
                    "upgrade_red",
                    cid=cid,
                    path=rq.target,
                    status=resp.status,
                    detail="handshake nao retornou 101",
                )
                body, framing = await read_response_body_raw(
                    up_rd, leftover, resp.headers, resp.status, self.cfg.max_html_bytes
                )
                out_pairs = [
                    (n, v)
                    for n, v in resp.headers.pairs
                    if n.lower() not in _HOP_BY_HOP
                ]
                if framing == "te":
                    out_pairs.append(("transfer-encoding", "chunked"))
                    body = chunk_encode(body)
                elif framing == "eof":
                    out_pairs.append(("connection", "close"))
                wire = (
                    resp.status_line.encode("latin-1")
                    + b"\r\n"
                    + b"".join(f"{n}: {v}\r\n".encode("latin-1") for n, v in out_pairs)
                    + b"\r\n"
                    + body
                )
                writer.write(wire)
                await writer.drain()
                await self.spy.event(
                    "res", cid=cid, path=rq.target, status=resp.status, bytes_=len(body)
                )
                return framing == "eof"
            # 101: relay the switching head (plus any first-frame stray) verbatim
            writer.write(resp.raw + b"\r\n\r\n" + leftover)
            await writer.drain()
            sc_hdr = resp.headers.get_first("set-cookie")
            await self.spy.event(
                "res",
                cid=cid,
                path=rq.target,
                status=101,
                upgrade=True,
                set_cookie=[
                    k.strip().split("=", 1)[0] for k in sc_hdr.split(";") if k.strip()
                ]
                if sc_hdr
                else [],
            )
            await self._pump(reader, writer, up_rd, up_wr, cid)
            return True
        finally:
            await self._close_upstream(up)

    async def _splice_101(
        self, resp, leftover, rq, up_rd, up_wr, reader, writer, cid
    ) -> None:
        """Corner case: upstream returns 101 without the client declaring
        an upgrade. Relay the switching head verbatim and splice; the client
        handles the protocol switch (or drops the connection, which is the
        honest outcome for a non-browser client). Logged with `declared:false`
        so the spy can distinguish it from a genuine handshake."""
        writer.write(resp.raw + b"\r\n\r\n" + leftover)
        await writer.drain()
        await self.spy.event(
            "res",
            cid=cid,
            path=rq.target,
            status=101,
            upgrade=True,
            declared=False,
        )
        await self._pump(reader, writer, up_rd, up_wr, cid)

    # ---------------------------------------------------------------- pumping
    async def _pump(self, a_rd, a_wr, b_rd, b_wr, cid) -> None:
        """Bidirectional byte pump until EITHER side closes.

        Teardown closes both connections (a switched connection is not
        reusable after half-close) and logs the aggregate.
        """
        stats = {"ab": 0, "ba": 0}

        async def dir_ab() -> None:
            while True:
                d = await a_rd.read(65536)
                if not d:
                    return
                b_wr.write(d)
                await b_wr.drain()
                stats["ab"] += len(d)

        async def dir_ba() -> None:
            while True:
                d = await b_rd.read(65536)
                if not d:
                    return
                a_wr.write(d)
                await a_wr.drain()
                stats["ba"] += len(d)

        tasks = [asyncio.ensure_future(dir_ab()), asyncio.ensure_future(dir_ba())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, OSError, ConnectionResetError):
                    pass
            for w in (a_wr, b_wr):
                try:
                    w.close()
                except (OSError, RuntimeError):
                    pass
            await self.spy.event(
                "ws_close",
                cid=cid,
                bytes_client_to_upstream=stats["ab"],
                bytes_upstream_to_client=stats["ba"],
            )

    # ============================================================== plumbing
    async def _open_upstream(self, cid):
        try:
            rd, wr = await asyncio.wait_for(
                asyncio.open_connection(self.cfg.upstream_host, self.cfg.upstream_port),
                timeout=10,
            )
            return (rd, wr)
        except (OSError, asyncio.TimeoutError) as exc:
            await self.spy.event(
                "err",
                cid=cid,
                stage="upstream-connect",
                msg=f"{type(exc).__name__}: {exc}",
            )
            return None

    async def _close_upstream(self, up) -> None:
        if up is None:
            return
        _rd, wr = up
        try:
            wr.close()
        except (OSError, RuntimeError):
            pass

    def _rewrite_location(self, location: str, host_seen: str) -> str:
        if not location.startswith(("http://", "https://")):
            return location
        p = urlsplit(location)
        auth = p.hostname or ""
        if p.port and p.port not in (80, 443):
            auth = f"{auth}:{p.port}"
        candidates = {
            self.cfg.upstream_authority,
            self.cfg.vhost_authority,
            self.cfg.upstream_host,
        }
        base_matches = {c.split(":")[0] for c in candidates}
        if auth.split(":")[0] not in base_matches and auth not in candidates:
            return location
        scheme = "https" if self.cfg.listen_port == 443 else "http"
        target = host_seen or "localhost"
        query = f"?{p.query}" if p.query else ""
        return f"{scheme}://{target}{p.path}{query}"

    # -------------------------------------------------------- synthetic reply
    async def _synthesize(self, writer, status, reason, headers, body, conn_close=True) -> None:
        reason_map = {
            200: "OK",
            201: "Created",
            204: "No Content",
            304: "Not Modified",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            405: "Method Not Allowed",
            413: "Payload Too Large",
            431: "Header Fields Too Long",
            500: "Internal Server Error",
            502: "Bad Gateway",
            503: "Service Unavailable",
        }
        reason = reason if reason else reason_map.get(status, "Error")
        hdrs = list(headers)
        if body and not any(n.lower() == "content-length" for n, _ in hdrs):
            hdrs.append(("content-length", str(len(body))))
        if conn_close and not any(n.lower() == "connection" for n, _ in hdrs):
            hdrs.append(("connection", "close"))
        wire = (
            f"HTTP/1.1 {status} {reason}\r\n".encode("latin-1")
            + b"".join(f"{n}: {v}\r\n".encode("latin-1") for n, v in hdrs)
            + b"\r\n"
            + (body or b"")
        )
        writer.write(wire)
        await writer.drain()

    async def _synthetic_json(self, writer, status, obj, extra=None) -> None:
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
        hdrs = [("content-type", "application/json; charset=utf-8")]
        if extra:
            hdrs += list(extra)
        await self._synthesize(writer, status, "", hdrs, body, conn_close=True)
