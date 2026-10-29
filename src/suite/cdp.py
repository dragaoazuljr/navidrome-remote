"""cdp.py — CDP mini-client (stdlib only) para o boot-capture (A2).

Guida um `google-chrome --headless` real (o dev-workspace tem Chrome) com
apenas biblioteca padrão:

  * descobre o endpoint CDP (stdout: "DevTools listening on ws://...");
  * HTTP: /json/new (PUT), /json/list, /json/close;
  * fala RFC6455 puro com o socket `/devtools/page/<id>`
    (cliente mascara; servidor não);
  * correlaciona comandos por `id` e bufferiza eventos;
  * `Tab` instala um hook de coleta (rede/console) alimentado pelo loop.

Domínios usados: Page, Network, Runtime, Log.

Segurança (invariante de credenciais):
  * valores de cookie/auth NUNCA ficam no rastro — só nomes;
  * JWTs, base64 longo e o sal `s=` das URLs Subsonic são mascarados.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

# ----------------------------------------------------------------- browser
# Zombie defense (incident 2026-10-29): a run killed mid-flight (dispatcher
# budget kill / OOM) orphaned its headless Chrome — ~10 browsers / ~4 GB
# accumulated on the 8 GB LXC and starved every later E2E run (flaky
# media events, spurious reds). Chrome therefore runs in its OWN process
# group (start_new_session) and registers a pgid marker; every launch
# sweeps STALE markers from dead runs first, and shutdown kills the whole
# group (main + renderers) and removes its own marker.
CHROME_MARKER_DIR = os.path.join(tempfile.gettempdir(), "nvra-chrome-pgids")


def _pgid_alive(pgid: int) -> bool:
    try:
        os.kill(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def sweep_stale_chrome_groups(except_pid: int = 0) -> int:
    """Kill process groups left behind by dead runs. Returns the number
    of living groups killed (0 = nothing stale)."""
    killed = 0
    try:
        entries = os.listdir(CHROME_MARKER_DIR)
    except FileNotFoundError:
        return 0
    for name in entries:
        if not name.endswith(".pgid"):
            continue
        path = os.path.join(CHROME_MARKER_DIR, name)
        try:
            with open(path, "r", encoding="ascii") as fh:
                pgid = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        if pgid == except_pid:
            continue
        if _pgid_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.unlink(path)
        except OSError:
            pass
    return killed


# ------------------------------------------------------------------ redact
CDP_REDACTIONS = (
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]{0,})?\b"),
     "<jwt:redigido>"),
    (re.compile(r"(s=)([A-Za-z0-9%/+=.-]{8,})"),
     "\\1<redigido>"),
    (re.compile(r"[A-Za-z0-9+/=]{40,}"),
     "<b64:redigido>"),
)

HEADER_WHITELIST = {
    "content-type", "content-length", "content-encoding", "transfer-encoding",
    "vary", "location", "range", "accept-ranges", "cache-control",
    "permissions-policy", "referrer-policy", "x-frame-options",
    "access-control-allow-origin", "access-control-allow-methods",
    "access-control-allow-headers", "access-control-expose-headers",
    "www-authenticate", "set-cookie", "upgrade", "connection",
    "sec-websocket-accept", "x-ratelimit-limit", "x-ratelimit-remaining",
}


def redact(text) -> str:
    """Remove material sensível de qualquer string que vá para o rastro."""
    if text is None:
        return ""
    out = str(text)
    for pat, repl in CDP_REDACTIONS:
        out = pat.sub(repl, out)
    return out[:2048]


def _select_headers(headers) -> list:
    out = []
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk not in HEADER_WHITELIST:
            continue
        if lk == "set-cookie":
            name = str(v).split("=", 1)[0].strip()
            out.append([k, f"{name}=***"])
            continue
        out.append([k, redact(str(v))])
    return out


def _now_ms() -> int:
    return int(time.time() * 1000)


# ------------------------------------------------------------------ frames
def _frame_build(opcode: int, payload: bytes, mask: bool = False) -> bytes:
    length = len(payload)
    b2 = 0x80 | 126 if length >= 126 and length < 65536 else \
        0x80 | 127 if length >= 65536 else 0x80 | length
    if mask:
        b2 |= 0x80
    if length < 126:
        header = bytes([0x80 | opcode, b2])
    elif length < 65536:
        header = bytes([0x80 | opcode, b2]) + length.to_bytes(2, "big")
    else:
        header = bytes([0x80 | opcode, b2]) + length.to_bytes(8, "big")
    if not mask:
        return header + payload
    key = os.urandom(4)
    return header + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


# ------------------------------------------------------------------ session
class CDPSession:
    """Conexão WS CDP de uma página: comandos + eventos + hook de coleta."""

    def __init__(self, ws_url: str, hook=None):
        self.ws_url = ws_url
        self.hook = hook  # async fn(method, params) — coleta do Tab
        self.rd: asyncio.StreamReader | None = None
        self.wr: asyncio.StreamWriter | None = None
        self._id = 0
        self._in_flight: dict = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.events: list = []
        self.closed = False

    async def connect(self) -> None:
        p = urllib.parse.urlsplit(self.ws_url)
        self.rd, self.wr = await asyncio.open_connection(p.hostname, p.port)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {p.path} HTTP/1.1\r\n"
            f"Host: {p.hostname}:{p.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.wr.write(req.encode())
        await self.wr.drain()
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(self.rd.read(4096), 15)
            if not chunk:
                raise ConnectionError("CDP: fechou antes do handshake WS")
            buf.extend(chunk)
        idx = buf.index(b"\r\n\r\n")
        first = bytes(buf[:idx]).split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in first + " ":
            raise ConnectionError(f"CDP: handshake falhou ({first})")
        # SO o residuo POS-header entra no leitor: bytes do 101 nao sao
        # frames WS (comeriam a sincronia do primeiro burst).
        self.reader_task = asyncio.ensure_future(
            self._reader_loop(bytearray(buf[idx + 4:])))

    async def aclose(self) -> None:
        if not self.closed and self.wr is not None:
            try:
                self.wr.write(_frame_build(0x8, b"", mask=True))
                await self.wr.drain()
            except (OSError, RuntimeError):
                pass
        self.closed = True
        if self.wr is not None:
            try:
                self.wr.close()
            except (OSError, RuntimeError):
                pass
        if getattr(self, "reader_task", None) is not None:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except (Exception, asyncio.CancelledError):
                pass

    @staticmethod
    def _frame_needed(buf) -> "int | None":
        """Bytes faltantes p/ completar o frame a frente (None = sem
        frame iniciado; 0 = completo)."""
        if len(buf) < 2:
            return 2 - len(buf)
        length = buf[1] & 0x7F
        pos = 2
        if length == 126:
            if len(buf) < 4:
                return 4 - len(buf)
            length = int.from_bytes(bytes(buf[2:4]), "big")
            pos = 4
        elif length == 127:
            if len(buf) < 10:
                return 10 - len(buf)
            length = int.from_bytes(bytes(buf[2:10]), "big")
            pos = 10
        if buf[1] & 0x80:
            if len(buf) < pos + 4:
                return pos + 4 - len(buf)
            pos += 4
        return (pos + length) - len(buf)

    async def _drain_frames(self, buf) -> bool:
        """Consome todos os frames COMPLETOS do buffer; True se >=1."""
        consumed = 0
        while len(buf) >= 2:
            need = self._frame_needed(buf)
            if need is None or need > 0:
                break
            opcode = buf[0] & 0x0F
            length = buf[1] & 0x7F
            pos = 2
            if length == 126:
                length = int.from_bytes(bytes(buf[2:4]), "big")
                pos = 4
            elif length == 127:
                length = int.from_bytes(bytes(buf[2:10]), "big")
                pos = 10
            mask = b""
            if buf[1] & 0x80:
                mask = bytes(buf[pos:pos + 4])
                pos += 4
            payload = bytes(buf[pos:pos + length])
            del buf[:pos + length]
            if mask:
                payload = bytes(
                    b ^ mask[i % 4] for i, b in enumerate(payload))
            await self._on_frame(opcode, payload)
            consumed += 1
        return consumed > 0

    async def _reader_loop(self, seed) -> None:
        """Loop SEM busy-spin: toda volta termina em await (ou na quebra
        do stream). Buffer incompleto -> bloqueia em read(65536)."""
        buf = bytearray(seed)
        try:
            while True:
                got_data = False
                await self._drain_frames(buf)
                if buf:
                    chunk = await self.rd.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    got_data = True
                else:
                    chunk = await self.rd.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    got_data = True
        finally:
            self.closed = True
            for item in self._in_flight.values():
                if not item["fut"].done():
                    item["fut"].set_exception(ConnectionError("CDP: fechado"))

    async def _on_frame(self, opcode: int, payload: bytes) -> None:
        if opcode == 0x8:
            self.closed = True
            return
        if opcode == 0x9:
            try:
                self.wr.write(_frame_build(0xA, payload, mask=True))
                await self.wr.drain()
            except (OSError, RuntimeError):
                pass
            return
        if opcode not in (0x1, 0x2):
            return
        try:
            msg = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return
        if "id" in msg and "method" not in msg:
            item = self._in_flight.pop(msg["id"], None)
            if item is not None and not item["fut"].done():
                item["fut"].set_result(msg)
            return
        if len(self.events) > 8000:
            del self.events[:1000]
        self.events.append({"ts": _now_ms(), "event": msg})
        await self.queue.put(msg)
        if self.hook is not None:
            task = asyncio.ensure_future(self._guarded_hook(msg))

    async def _guarded_hook(self, msg) -> None:
        try:
            await self.hook(msg.get("method", ""), msg.get("params") or {})
        except Exception:
            pass  # coleta nunca derruba a sessão

    # -------------------------------------------------------------- commands
    async def send(self, method: str, params=None, timeout: float = 30.0):
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._in_flight[mid] = {"fut": fut, "ts": time.monotonic()}
        payload = json.dumps({"id": mid, "method": method,
                              "params": params or {}}).encode()
        try:
            self.wr.write(_frame_build(0x1, payload, mask=True))
            await self.wr.drain()
        except (OSError, RuntimeError) as exc:
            self._in_flight.pop(mid, None)
            raise ConnectionError(f"CDP: falha enviando {method}: {exc}") from exc
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._in_flight.pop(mid, None)
            raise

    async def enable(self, *domains) -> None:
        for d in domains:
            await self.send(f"{d}.enable")

    async def wait_event(self, pred, timeout: float = 30.0):
        """pred(method, params) -> bool; retorna o evento ou None."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(self.queue.get(), remaining)
            except asyncio.TimeoutError:
                return None
            if pred(msg.get("method", ""), msg.get("params") or {}):
                return msg

    async def wait_for_navigation(self, event_pred, send_coro, timeout: float = 30.0):
        """Corre a navegação: evento de load OU resposta do comando."""
        ev_fut = asyncio.ensure_future(
            self.wait_event(event_pred, timeout=timeout))
        nav_resp = None
        try:
            nav_resp = await send_coro
        except Exception as exc:  # navigate pode responder depois do evento
            nav_resp = {"error": str(exc)}
        ev = None
        if not ev_fut.done():
            try:
                ev = await asyncio.wait_for(ev_fut, 2.0)
            except (asyncio.TimeoutError, Exception):
                ev = None
        else:
            ev = await ev_fut
        return ev, nav_resp


# ------------------------------------------------------------------ driver
class BrowserDriver:
    """Chrome headless + tabs (A2)."""

    def __init__(self, chrome_bin: str = "/usr/bin/google-chrome"):
        self.chrome_bin = chrome_bin
        self.proc: subprocess.Popen | None = None
        self.origin = ""
        self.udd = ""
        self.tabs: list = []

    def launch(self, port: int | None = None) -> None:
        self.udd = os.path.join(tempfile.gettempdir(), f"nvra-chrome-{os.getpid()}")
        os.makedirs(self.udd, exist_ok=True)
        port = port or _free_port()
        flags = ["--headless=new", "--disable-gpu",
                 "--no-first-run", "--no-default-browser-check",
                 "--disable-dev-shm-usage",
                 # test-harness determinism: the harness drives the player
                 # with programmatic clicks (no real user activation), so
                 # the autoplay policy would veto play()/pause() round
                 # trips and hide the very events under test.
                 "--autoplay-policy=no-user-gesture-required",
                 f"--remote-debugging-port={port}", f"--user-data-dir={self.udd}"]
        if os.geteuid() == 0:
            # root (container/LXC): Chrome refuses to run without disabling
            # the sandbox setuid machinery.
            flags += ["--no-sandbox", "--disable-setuid-sandbox"]
        os.makedirs(CHROME_MARKER_DIR, exist_ok=True)
        n_killed = sweep_stale_chrome_groups()
        if n_killed:
            print(f"[cdp] varreu {n_killed} grupo(s) Chrome órfeno(s) "
                  f"de execuções mortas antes do launch", flush=True)
        self.proc = subprocess.Popen(
            [self.chrome_bin] + flags,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True,
            start_new_session=True,  # own process group (zombie defense)
        )
        try:
            with open(os.path.join(
                    CHROME_MARKER_DIR, f"{self.proc.pid}.pgid"),
                    "w", encoding="ascii") as fh:
                fh.write(str(self.proc.pid))
        except OSError:
            pass
        deadline = time.time() + 25
        banner = ""
        leftovers = []
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    break
                continue
            if "DevTools listening on" in line:
                banner = line.strip()
                break
            if len(leftovers) < 12:
                leftovers.append(line)
        if not banner:
            raise RuntimeError(
                "Chrome: sem banner CDP. Últimas linhas:\n"
                + "".join(leftovers))
        wsurl = banner.split(" ", 3)[-1].strip()
        p = urllib.parse.urlsplit(wsurl)
        self.origin = f"http://127.0.0.1:{p.port}"

    def shutdown(self) -> None:
        proc = self.proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except OSError:
                pass
            # kill the WHOLE group (renderers are children of the group
            # leader) and dissolve the marker — no orphans on exit.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if self.udd:
            shutil.rmtree(self.udd, ignore_errors=True)
        if proc is not None:
            try:
                os.unlink(os.path.join(
                    CHROME_MARKER_DIR, f"{proc.pid}.pgid"))
            except OSError:
                pass

    def new_tab(self, url: str, loop: asyncio.AbstractEventLoop) -> "Tab":
        q = urllib.parse.quote(url, safe="")
        info = None
        for meth in ("PUT", "GET"):
            try:
                req = urllib.request.Request(
                    f"{self.origin}/json/new?{q}", method=meth)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    info = json.loads(resp.read())
                break
            except Exception:
                continue
        if info is None:
            raise RuntimeError("CDP: não consegui criar aba")
        tab = Tab(info, loop)
        self.tabs.append(tab)
        return tab


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------- tab
class Tab:
    """Página CDP com coletores de rede/console (A2)."""

    def __init__(self, page_info: dict, loop: asyncio.AbstractEventLoop):
        self.page_id = page_info["id"]
        self.loop = loop
        self.session = CDPSession(page_info["webSocketDebuggerUrl"],
                                  hook=self.collect)
        self.req_index: dict = {}
        self.responses: dict = {}
        self.mark_ids: list = []
        self._seen_before: "set[str]" = set()
        self.ws_created = 0
        self.ws_opened = 0
        self.ws_frames_in = 0
        self.ws_frames_out = 0
        self.ws_urls: list = []
        self.console: list = []
        self.failures: list = []
        self.nav_log: list = []

    async def start(self) -> None:
        await self.session.connect()
        await self.session.enable("Page", "Network", "Runtime", "Log")
        await self.session.send("Network.enable", {"maxPostDataSize": 0})

    async def aclose(self) -> None:
        await self.session.aclose()

    # ----------------------------------------------------------- collectors
    async def collect(self, method: str, params: dict) -> None:
        if method == "Network.requestWillBeSent":
            req = params.get("request") or {}
            rid = params.get("requestId", "")
            self.req_index[rid] = {
                "id": rid,
                "ts": _now_ms(),
                "method": req.get("method", "?"),
                "url": redact(req.get("url", "")),
                "resourceType": params.get("type", "?"),
                "fromCache": params.get("fromCache", False),
                "reqHeadersKept": {
                    k: redact(v)
                    for k, v in (req.get("headers") or {}).items()
                    if k.lower() in ("range", "accept", "referer", "origin",
                                     "content-type")
                },
            }
            if rid not in self.mark_ids:
                self.mark_ids.append(rid)
        elif method == "Network.responseReceived":
            resp = params.get("response") or {}
            rid = params.get("requestId", "")
            hdr_kept = _select_headers(resp.get("headers"))
            self.responses[rid] = {
                "status": resp.get("status", -1),
                "mimeType": resp.get("mimeType", ""),
                "contentLength": resp.get("contentLength", 0),
                "headers": hdr_kept,
                "setCookieNames": [
                    h[1].split("=", 1)[0] for h in hdr_kept
                    if h[0].lower() == "set-cookie"
                ],
            }
        elif method == "Network.webSocketCreated":
            self.ws_created += 1
            self.ws_urls.append(redact(params.get("url", "")))
        elif method == "Network.webSocketOpened":
            self.ws_opened += 1
        elif method == "Network.webSocketFrameReceived":
            self.ws_frames_in += 1
        elif method == "Network.webSocketFrameSent":
            self.ws_frames_out += 1
        elif method == "Network.loadingFailed":
            rec = self.req_index.get(params.get("requestId", "")) or {}
            self.failures.append({
                "ts": _now_ms(), "kind": "loading-failed",
                "url": redact(rec.get("url", "")),
                "error": redact(params.get("errorText", "")),
            })
        elif method == "Runtime.consoleAPICalled":
            parts = []
            for a in (params.get("args") or []):
                if not isinstance(a, dict):
                    continue
                if a.get("value") is not None:
                    parts.append(redact(a["value"]))
                else:
                    parts.append(redact(a.get("description") or a.get("type")))
            self.console.append({
                "ts": _now_ms(), "level": params.get("type", "log"),
                "text": " | ".join(x for x in parts if x),
            })
            if len(self.console) > 400:
                del self.console[:200]
        elif method == "Log.entryAdded":
            e = params.get("entry") or {}
            self.console.append({
                "ts": _now_ms(), "level": e.get("level", "warn"),
                "text": redact(e.get("message", "")), "src": e.get("source", ""),
            })
        elif method == "Page.frameNavigated":
            fr = params.get("frame") or {}
            if not fr.get("parentId"):
                self.nav_log.append({
                    "ts": _now_ms(), "url": redact(fr.get("url", ""))
                })

    # --------------------------------------------------------- operations
    def mark(self) -> None:
        """Congela o acumulado ate aqui: janelas posteriores
        (requests_since_mark) so veem o que chegar apos o marco."""
        self._seen_before = set(self.mark_ids)

    async def navigate(self, url: str, timeout: float = 30.0):
        self.mark()
        self.nav_log.append({"ts": _now_ms(), "intent": url})
        coro = asyncio.ensure_future(self.session.send(
            "Page.navigate", {"url": url}))
        ev, resp = await self.session.wait_for_navigation(
            lambda m, p: m == "Page.loadEventFired", coro, timeout=timeout)
        return ev, resp

    async def reload(self, timeout: float = 30.0):
        self.mark()
        self.nav_log.append({"ts": _now_ms(), "intent": "reload"})
        coro = asyncio.ensure_future(self.session.send("Page.reload"))
        await self.session.wait_for_navigation(
            lambda m, p: m == "Page.loadEventFired", coro, timeout=timeout)

    async def eval(self, expr: str, timeout: float = 20.0):
        """Expressão JS serializável -> valor (ou None).

        O wrapper converte exceções em string 'EXC::...', então erros de
        página voltam como valor; erros de transporte do CDP levantam
        ConnectionError/asyncio.TimeoutError.
        """
        # expressoes aqui sao IIFE/flecha (comecam com '(') -> parendo
        # simples segura; excecoes da pagina viram 'EXC::...'
        wrapped = (
            "(function(){ try { return (%s); }"
            " catch (e) { return 'EXC::' + String(e && e.message); } })()"
        ) % expr
        resp = await self.session.send(
            "Runtime.evaluate",
            {"expression": wrapped, "returnByValue": True},
            timeout=timeout)
        # resposta CDP: {id, result:{result: RemoteObject}}
        ro = (resp.get("result") or {}).get("result") or {}
        if ro.get("type") == "undefined" or ro.get("value") is None:
            return None
        return ro.get("value")

    async def get_cookie_names(self) -> list:
        resp = await self.session.send("Storage.getCookies")
        out = []
        for c in resp.get("cookies", []):
            out.append({
                "domain": c.get("domain", ""), "path": c.get("path", ""),
                "name": c.get("name", ""), "httpOnly": c.get("httpOnly", False),
            })
        return out

    # ----------------------------------------------------------- snapshots
    def _with_resp(self, rid: str) -> dict:
        rec = dict(self.req_index[rid])
        resp = self.responses.get(rid)
        if resp:
            rec["resp"] = resp
        return rec

    def requests_since_mark(self) -> list:
        out = [self._with_resp(rid) for rid in self.mark_ids
               if rid in self.req_index and rid not in self._seen_before]
        out.sort(key=lambda x: x["ts"])
        return out

    def all_requests(self) -> list:
        out = [self._with_resp(rid) for rid, _ in sorted(
            self.req_index.items(), key=lambda kv: kv[1]["ts"])]
        out.sort(key=lambda x: x["ts"])
        return out
