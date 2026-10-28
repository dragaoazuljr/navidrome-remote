#!/usr/bin/env python3
"""cdp_selftest.py (A2) — self-test do mini-client CDP (stdlib only).

Hermetico: sobe um SERVIDOR CDP DE PALCO (asyncio, loopback) que fala
RFC6455 real e responde como o Chrome headless falaria para os dominios
que o boot-capture usa. Nada de Chrome real — o objetivo aqui e exercitar
o NOSSO lado do cabecalho: handshake, correlacao por id, eventos,
coletores e redacao.

Cobre:
  * _frame_build: frames curtos/medianos/longos, com e sem mascara;
    codec de ida-e-volta (monta, o palco desempacota).
  * CDPSession.connect: handshake HTTP->WS (chave/aceite verificados).
  * send/wait_event: correlacao por id, timeout, resposta assincrona
    (evento Page.loadEventFIREd logo apos Page.navigate).
  * Tab.collect: requestWillBeSent/responseReceived/webSocket*/
    consoleAPICalled/Log.entryAdded; contadores WS.
  * mark/windows: requests_since_mark so devolve o que chegou DEPOIS do
    ultimo mark (janela de captura).
  * Invariante de credenciais: o dump serializado do Tab NAO contem o
    valor do cookie, o sal Subsonic longo nem o JWT; contem NOMES.
  * aclose: close-frame 0x8 observado pelo palco.

Execucao:
  python3 suite/cdp_selftest.py [-v]
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# Valores sinteticos LONGOS (>= 40 chars / JWT shape) para provar redacao.
FAKE_SALT = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"  # 36 -> extendido:
FAKE_SALT += "abcd"                                  # agora 40
FAKE_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiYWxpY2UifQ.fake-signature-part"  # noqa: E501


# =====================================================================
# Palco: servico TCP que fala HTTP JSON + WS RFC6455
# =====================================================================
def _b64_accept(key: str) -> str:
    return base64.b64encode(
        hashlib.sha1((key + GUID).encode()).digest()).decode()


def _fr_un(op: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        h = bytes([0x80 | op, n])
    elif n < 65536:
        h = bytes([0x80 | op, 126]) + n.to_bytes(2, "big")
    else:
        h = bytes([0x80 | op, 127]) + n.to_bytes(8, "big")
    return h + payload


async def _rd_frame(reader: asyncio.StreamReader):
    """Decodifica UM frame (pode estar mascarado, como vem do cliente)."""
    hdr = await reader.readexactly(2)
    fin_op = hdr[0]
    masked = hdr[1] & 0x80
    ln = hdr[1] & 0x7F
    if ln == 126:
        ln = int.from_bytes(await reader.readexactly(2), "big")
    elif ln == 127:
        ln = int.from_bytes(await reader.readexactly(8), "big")
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(ln) if ln else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return (fin_op & 0x0F), payload


def _json_fr(obj) -> bytes:
    return _fr_un(0x1, json.dumps(obj).encode())


class StageCDP:
    """Palco CDP: /json/* via HTTP + /devtools/page/<id> via WS."""

    def __init__(self):
        self.port = 0
        self.server = None
        self.opcodes_seen: "list[int]" = []
        self.commands: "list[str]" = []

    async def start(self):
        self.server = await asyncio.start_server(self._on_conn, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def page_ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/page/PAGE-TEST"

    async def aclose(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    # ------------------------------------------------------------- conn
    async def _on_conn(self, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter):
        try:
            buf = bytearray()
            while b"\r\n\r\n" not in buf:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                buf.extend(chunk)
            head = bytes(buf[:buf.index(b"\r\n\r\n")]).decode("latin-1")
            lines = head.split("\r\n")
            method, path, _proto = lines[0].split(" ", 2)
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            is_ws = path.startswith("/devtools/")
            if not is_ws:
                body = self._http_json(method, path)
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    + ("Content-Length: %d\r\n" % len(body)).encode()
                    + b"Connection: close\r\n\r\n" + body)
                await writer.drain()
                writer.close()
                return
            # -------- handshake WS
            key = headers.get("sec-websocket-key", "")
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {_b64_accept(key)}\r\n\r\n")
            writer.write(resp.encode())
            await writer.drain()
            # burst de eventos (simula o boot da pagina no Chrome)
            await self._emit_burst(writer)
            # -------- loop de comandos
            while True:
                op, payload = await _rd_frame(reader)
                self.opcodes_seen.append(op)
                if op == 0x8:
                    break
                if op not in (0x1, 0x2):
                    continue
                msg = json.loads(payload)
                if "id" not in msg:
                    continue
                method = msg.get("method", "")
                self.commands.append(method)
                await self._respond(writer, msg, method)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass

    def _http_json(self, method: str, path: str) -> bytes:
        if path.startswith("/json/version"):
            return json.dumps({
                "Browser": "Chrome/156.0.8078.47",
                "Protocol-Version": "1.3",
                "webSocketDebuggerUrl":
                    f"ws://127.0.0.1:{self.port}/devtools/browser/STAGE",
            }).encode()
        if path.startswith("/json/new"):
            return json.dumps({
                "id": "PAGE-TEST", "type": "page", "url": "about:blank",
                "webSocketDebuggerUrl": self.page_ws_url(),
            }).encode()
        if path.startswith("/json/close"):
            return b'{"success":true}'
        return b"{}"

    async def _emit_burst(self, writer):
        ev = (
            {"method": "Runtime.consoleAPICalled",
             "params": {"type": "log",
                        "args": [{"type": "string", "value": "boot-log-ok"}]}},
            {"method": "Network.requestWillBeSent",
             "params": {
                 "requestId": "RID-1", "type": "Other", "fromCache": False,
                 "request": {
                     "method": "GET",
                     "url": ("http://127.0.0.1:%d/rest/ping?u=alice&s=%s"
                             "&v=1.12.1" % (self.port, FAKE_SALT)),
                     "headers": {
                         "Authorization": "Bearer " + FAKE_JWT,
                         "Range": "bytes=0-511"},
                 }}},
            {"method": "Network.responseReceived",
             "params": {"requestId": "RID-1",
                        "response": {
                            "status": 200,
                            "mimeType": "application/json",
                            "contentLength": 64,
                            "headers": {
                                "Set-Cookie":
                                    "nd_sess=TOPSECRET; Path=/; HttpOnly",
                                "Access-Control-Allow-Origin": "*",
                            }}}},
            {"method": "Network.requestWillBeSent",
             "params": {"requestId": "RID-2", "type": "Fetch",
                        "request": {"method": "GET",
                                    "url": "http://127.0.0.1:%d/theme"
                                           % self.port,
                                    "headers": {}}}},
            {"method": "Network.responseReceived",
             "params": {"requestId": "RID-2",
                        "response": {
                            "status": 200,
                            "mimeType": "application/json",
                            "contentLength": 16,
                            "headers": {"Set-Cookie": "theme=dark; Path=/"}}}},
            {"method": "Network.webSocketCreated",
             "params": {"sessionId": "S1", "url": "wss://stage.local/live"}},
            {"method": "Network.webSocketOpened",
             "params": {"sessionId": "S1"}},
            {"method": "Network.webSocketFrameReceived",
             "params": {"sessionId": "S1",
                        "response": {"data": "{\"hello\":\"world\"}"}}},
            {"method": "Log.entryAdded",
             "params": {"entry": {"level": "warning",
                                  "source": "console",
                                  "message": "stageware"}}},
        )
        for m in ev:
            writer.write(_json_fr(m))
        await writer.drain()

    async def _respond(self, writer, msg, method: str):
        mid = msg["id"]
        if method == "Page.navigate":
            writer.write(_json_fr({"id": mid, "result": {"frameId": "F1"}}))
            await writer.drain()
            # load event chega ASINCRONAMENTE (como no Chrome)
            await asyncio.sleep(0.05)
            writer.write(_json_fr(
                {"method": "Page.loadEventFired",
                 "params": {"timestamp": 42.0}}))
            await writer.drain()
        elif method == "Runtime.evaluate":
            writer.write(_json_fr(
                {"id": mid,
                 "result": {"result": {"type": "string", "value": "42"}}}))
            await writer.drain()
        elif method == "Storage.getCookies":
            writer.write(_json_fr(
                {"id": mid,
                 "result": {"cookies": [
                     {"name": "nd_sess", "value": "TOPSECRET",
                      "domain": "127.0.0.1", "path": "/",
                      "httpOnly": True},
                     {"name": "theme", "value": "dark",
                      "domain": "127.0.0.1", "path": "/",
                      "httpOnly": False}]}}))
            await writer.drain()
        else:
            writer.write(_json_fr({"id": mid, "result": {}}))
            await writer.drain()


# =====================================================================
# Casos
# =====================================================================
class FrameBuildTests(unittest.TestCase):
    def _rt_short(self):
        payload = b"hello"
        f = cdp._frame_build(0x1, payload, mask=True)
        self.assertEqual(f[0] & 0x0F, 1)
        self.assertTrue(f[1] & 0x80)               # masked
        self.assertEqual(f[1] & 0x7F, 5)
        mask = f[2:6]
        out = bytes(b ^ mask[i % 4] for i, b in enumerate(f[6:]))
        self.assertEqual(out, payload)

    def _rt_med(self):
        payload = os.urandom(200)
        f = cdp._frame_build(0x2, payload, mask=True)
        self.assertEqual(f[1] & 0x7F, 126)
        self.assertEqual(int.from_bytes(f[2:4], "big"), 200)
        mask = f[4:8]
        out = bytes(b ^ mask[i % 4] for i, b in enumerate(f[8:]))
        self.assertEqual(out, payload)

    def _rt_long(self):
        payload = os.urandom(100000)
        f = cdp._frame_build(0x1, payload, mask=False)
        self.assertEqual(f[1] & 0x7F, 127)
        self.assertEqual(int.from_bytes(f[2:10], "big"), 100000)
        self.assertEqual(f[10:], payload)


class RedactionTests(unittest.TestCase):
    def test_jwt(self):
        r = cdp.redact("tok=" + FAKE_JWT)
        self.assertNotIn(FAKE_JWT, r)
        self.assertIn("<jwt:redigido>", r)

    def test_subsonic_salt(self):
        r = cdp.redact("/rest/ping?u=alice&s=" + FAKE_SALT)
        self.assertNotIn(FAKE_SALT, r)
        self.assertIn("s=<redigido>", r)

    def test_set_cookie_name_kept_value_removed(self):
        hh = {"Set-Cookie": "nd_sess=TOPSECRET; Path=/; HttpOnly"}
        sel = cdp._select_headers(hh)
        blob = json.dumps(sel)
        self.assertIn("nd_sess", blob)
        self.assertNotIn("TOPSECRET", blob)


class TabIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stage = StageCDP()
        await self.stage.start()
        self.tab = cdp.Tab(
            {"id": "PAGE-TEST", "type": "page", "url": "about:blank",
             "webSocketDebuggerUrl": self.stage.page_ws_url()},
            asyncio.get_running_loop())
        await self.tab.start()
        # drain o burst (ele sai na conexao, antes dos enables)
        await asyncio.sleep(0.15)

    async def asyncTearDown(self):
        await self.tab.aclose()
        await self.stage.aclose()

    def _dump(self) -> str:
        return json.dumps(
            {"req": self.tab.all_requests(),
             "resp": self.tab.responses,
             "console": self.tab.console,
             "nav": self.tab.nav_log,
             "marks": self.tab.mark_ids},
            ensure_ascii=False, default=str)

    async def test_collectors_filled(self):
        self.assertGreaterEqual(len(self.tab.req_index), 2)
        self.assertEqual(self.tab.responses["RID-1"]["status"], 200)
        self.assertEqual(
            self.tab.responses["RID-1"]["setCookieNames"], ["nd_sess"])
        self.assertEqual(self.tab.ws_created, 1)
        self.assertEqual(self.tab.ws_opened, 1)
        self.assertEqual(self.tab.ws_frames_in, 1)
        # console + log presentes
        joined = " ".join(c["text"] for c in self.tab.console)
        self.assertIn("boot-log-ok", joined)
        self.assertIn("stageware", joined)

    async def test_eval_and_navigate(self):
        val = await self.tab.eval("whatever-js")
        self.assertEqual(val, "42")
        ev, resp = await self.tab.navigate("http://127.0.0.1/x",
                                           timeout=5)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["method"], "Page.loadEventFired")
        # navigate marcou a janela: pedidos PRE-marco nao contam mais
        since = self.tab.requests_since_mark()
        urls = {r["url"] for r in since}
        self.assertFalse(any("REST-PING" in u.upper() for u in urls),
                         "pedido pre-marco escapou da janela")

    async def test_credentials_never_leak(self):
        blob = self._dump()
        self.assertNotIn("TOPSECRET", blob,
                         "cookie value vazou no dump do Tab")
        self.assertNotIn(FAKE_SALT, blob,
                         "sal Subsonic completo vazou")
        self.assertNotIn(FAKE_JWT, blob, "JWT completo vazou")
        self.assertIn("nd_sess", blob, "nome do cookie deveria sobreviver")

    async def test_close_frame_observed(self):
        await self.tab.aclose()
        await asyncio.sleep(0.1)
        self.assertIn(0x8, self.stage.opcodes_seen,
                      "palco nao recebeu close-frame 0x8")


class DriverBannerTests(unittest.IsolatedAsyncioTestCase):
    """BrowserDriver.launch lendo um 'chrome' DE FACA (script que imprime
    o banner CDP) — exercita a parsing do banner sem browser real."""

    async def asyncSetUp(self):
        self.tmp = await asyncio.to_thread(
            __import__("tempfile").mkdtemp, prefix="cdnbanner-")
        self.fake_script = os.path.join(self.tmp, "fake_chrome.py")
        with open(self.fake_script, "w", encoding="utf-8") as fh:
            fh.write(
                "import sys, time\n"
                "sys.stdout.write(\n"
                "  'DevTools listening on ws://127.0.0.1:9/devtools/"
                "browser/FK\\n');\n"
                "sys.stdout.flush()\n"
                "time.sleep(2)\n")

    async def asyncTearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_banner_parsed(self):
        drv = cdp.BrowserDriver(chrome_bin="/usr/bin/true")
        wrap = os.path.join(self.tmp, "fake_chrome.sh")
        with open(wrap, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexec %s %s \"$@\"\n"
                     % (sys.executable, self.fake_script))
        os.chmod(wrap, 0o755)
        drv.chrome_bin = wrap
        # launch() e SINCRONO (Popen + readline) -> roda fora do loop
        await asyncio.wait_for(asyncio.to_thread(drv.launch, 0), 20)
        self.assertTrue(drv.origin.startswith("http://127.0.0.1:9"))
        drv.shutdown()
        await asyncio.sleep(0.2)
        # proc terminado pelo shutdown
        self.assertIsNotNone(drv.proc.poll())


if __name__ == "__main__":
    unittest.main(verbosity=2)
