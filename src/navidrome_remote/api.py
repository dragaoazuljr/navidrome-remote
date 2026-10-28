"""The SBC control API: /api/v1/* (spec v1.2, componente B — M1/A3).

Routes (complete contract, card A3)
-----------------------------------
  GET  /api/v1/health    PUBLIC liveness probe (never gated).
  GET  /api/v1/status    playback state + queue/settings view.
  POST /api/v1/play      play a song id (server-built stream URL) or an
                         explicit url (M0' compat; see design doc).
  POST /api/v1/pause     pause.
  POST /api/v1/resume    unpause.
  POST /api/v1/next      playlist-next.
  POST /api/v1/prev      playlist-prev (`previous` accepted as alias).
  POST /api/v1/stop      stop (playlist intent is remembered by the store).
  POST /api/v1/seek      {"pos": seconds}.
  POST /api/v1/shuffle   {"value": bool} or bare call = toggle.
  POST /api/v1/repeat    {"mode": off|track|playlist} (aliases: all, one).
  POST /api/v1/volume    {"level": 0..100}.
  GET  /api/v1/queue     sanitized queue view (id/title — no URLs).

Concrete shapes (M1 contract)
-----------------------------
  GET /status -> {phase, engine:"connected"|"down", idle,
                  play:{id,title,pos}|null, playing, paused,
                  shuffle:boolean, repeat:"off"|"track"|"playlist",
                  volume:int|null, queue:{length,index}}
  GET /queue  -> {items:[{id,title?}...], settings:{index,volume,
                  shuffle,repeat}, engine}
  next/prev while the engine is IDLE (no active track) -> 503
  engine-idle (a kiosk command cannot ride a stopped engine); a DEAD
  engine is reported as engine-down.
  POST /api/v1/queue     {"items":[{id,title?}...], "mode": replace|append}.
  POST /api/v1/spy       client-side beacon (bridge.js); carries the
                         session `jwt` in the body (sendBeacon cannot set
                         headers — see gateway.L1).

Authorization (three-layer session gate — see gateway.py)
----------------------------------------------------------
Default posture (NVRA_API_TOKEN empty): EVERY route except /health
requires an ACTIVE Navidrome session (L1 presence, L2 not-the-service-
account, L3 per-call liveness probe against the upstream). The static
NVRA_API_TOKEN is an OPTIONAL L0: a valid static bearer authorizes the
call WITHOUT a Navidrome session (machine-to-machine escape). When the
token is CONFIGURED, an INVALID static is rejected outright — except a
signed JWT travels with its own credential and still rides L1..L3.
L0 never applies to the service account itself (no account to bind —
that is the point).

Credential invariant (enforced by construction here)
----------------------------------------------------
Responses contain only machine codes, titles and counters. No stream
URL, no token, no salt, no credential of any kind crosses to a client:
* `play` no longer echoes the composed URL (M0' `file_set` removed);
* `queue` exchanges ids/titles only; URLs are rebuilt server-side;
* the spy records names, never values (M0' rule, preserved).
"""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import urlencode

from . import NAVIDROME_ANCHOR, SERVICE_VERSION, SUBSONIC_CLIENT_VER
from .ipc import EngineDown
from .queue import MAX_ITEMS, QueueStore, StoreError
from .spy import Spy

PHASE = "M1"


def _json(status: int, payload: dict) -> "tuple[int, str, bytes]":
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return status, "application/json; charset=utf-8", body


def subsonic_stream_url(upstream_base: str, song_id: str, user: str, password: str) -> str:
    """Navidrome/Subsonic `stream` URL for a song id.

    Salt = first 12 chars of base64(password) (Subsonic spec). Built
    SERVER-SIDE ONLY; the URL never appears in any client response.
    """
    salt = base64.b64encode(password.encode()).decode()[:12]
    params = {
        "u": user,
        "s": salt,
        "v": SUBSONIC_CLIENT_VER,
        "c": "navidrome-remote",
        "id": song_id,
    }
    return f"{upstream_base}/rest/stream?{urlencode(params)}"


REPEAT_MODES = {
    "off": ("off", 0, 0),
    "track": ("track", -1, 0),
    "playlist": ("playlist", 0, -1),
    # aliases (Navidrome UI spelling)
    "all": ("playlist", 0, -1),
    "one": ("track", -1, 0),
}


EXPECTED_ROUTES = (
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/status"),
    ("POST", "/api/v1/play"),
    ("POST", "/api/v1/pause"),
    ("POST", "/api/v1/resume"),
    ("POST", "/api/v1/next"),
    ("POST", "/api/v1/prev"),
    ("POST", "/api/v1/previous"),
    ("POST", "/api/v1/stop"),
    ("POST", "/api/v1/seek"),
    ("POST", "/api/v1/shuffle"),
    ("POST", "/api/v1/repeat"),
    ("POST", "/api/v1/volume"),
    ("GET", "/api/v1/queue"),
    ("POST", "/api/v1/queue"),
    ("POST", "/api/v1/spy"),
)


class Api:
    AVAILABLE = tuple("%s %s" % (m, p) for m, p in EXPECTED_ROUTES)

    def __init__(self, cfg, spy: Spy, engine, store: "QueueStore | None" = None,
                 gateway=None):
        self.cfg = cfg
        self.spy = spy
        self.engine = engine
        if store is None:
            store = QueueStore(cfg, spy)
        self.store = store
        if gateway is None:
            from .gateway import SessionGate
            gateway = SessionGate(cfg, spy)
        self.gateway = gateway

    # ------------------------------------------------------------ dispatcher
    async def handle(
        self, method: str, target: str, headers, body: bytes
    ) -> "tuple[int, str, bytes]":
        path = target.split("?", 1)[0]
        if path.endswith("/") and path != "/":
            path = path.rstrip("/")
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid-json"})
        if not isinstance(payload, dict):
            return _json(400, {"error": "payload-nao-e-objeto"})

        # ---- L0: explicit operator escape hatch (machine-to-machine).
        # A VALID static bearer authorizes the call WITHOUT a Navidrome
        # session — full exemption from L1..L3 (that is the design: the
        # token IS the credential; there is no browser session to check).
        # An invalid/absent static with no other credential is rejected
        # here, before anything else.
        static_exempt = False
        if self.cfg.api_token:
            auth = (headers.get_first("authorization") or "").strip().lower()
            static_ok = auth == "bearer " + self.cfg.api_token.lower()
            if not static_ok:
                # the static token is the ONLY thing presented: reject it
                # outright (no session to borrow strength from).
                from .gateway import extract_candidate
                cand = extract_candidate(headers, target, payload)
                if cand is None or cand.channel != "jwt":
                    await self.spy.event("api_denied", path=path, reason="token")
                    return _json(401, {"error": "unauthorized"})
            else:
                static_exempt = True

        # ---- L1..L3: session gate (health is the single public route;
        # a static-exempt call skips the session layers by design).
        if path != "/api/v1/health" and not static_exempt:
            verdict = await self.gateway.evaluate(headers, target, payload)
            if not verdict.allowed:
                return _json(
                    verdict.status,
                    {"error": verdict.code,
                     "detail": verdict.detail,
                     "channel": verdict.channel or "-"},
                )

        # method/route discipline: known path, wrong verb -> 405 (+Allow).
        expected_methods = [m for m, p in EXPECTED_ROUTES if p == path]
        if expected_methods and method not in expected_methods:
            allow = ", ".join(sorted(set(expected_methods)))
            await self.spy.event("api_method_not_allowed", method=method, path=path)
            body = json.dumps({"error": "method-not-allowed",
                                "allow": allow},
                               separators=(",", ":"), ensure_ascii=False)
            return (405, "application/json; charset=utf-8", body.encode())

        try:
            resp = await self._dispatch(method, path, payload)
            if resp is not None:
                return resp
        except EngineDown as exc:
            await self.spy.event("api_engine_down", path=path, error=str(exc))
            return _json(
                503,
                {"error": "engine-down",
                 "detail": "socket IPC inacessível — unidade mpv (ou mock) não está no ar"},
            )
        except RuntimeError as exc:
            await self.spy.event("api_rejected", path=path, error=str(exc))
            return _json(502, {"error": "engine-rejected", "detail": str(exc)})
        except StoreError as exc:
            await self.spy.event("api_store_rejected", path=path, error=str(exc))
            return _json(400, {"error": "queue-" + str(exc).lower()})

        await self.spy.event("api_unknown_route", method=method, path=path)
        return _json(404, {"error": "unknown-endpoint", "available": list(self.AVAILABLE)})

    async def _dispatch(self, method: str, path: str, payload: dict):
        if method == "GET" and path == "/api/v1/health":
            return await self._health()
        if method == "GET" and path == "/api/v1/status":
            return await self._status()
        if method == "GET" and path == "/api/v1/queue":
            return await self._queue_get()
        if method == "POST" and path == "/api/v1/queue":
            return await self._queue_post(payload)
        if method == "POST" and path == "/api/v1/play":
            return await self._play(payload)
        if method == "POST" and path == "/api/v1/pause":
            return await self._toggle_pause(True)
        if method == "POST" and path == "/api/v1/resume":
            return await self._toggle_pause(False)
        if method == "POST" and path in ("/api/v1/next",):
            if not await self._engine_has_track():
                await self.spy.event("api_engine_idle", path=path)
                return _json(503, {"error": "engine-idle",
                                   "detail": "motor ocioso — nenhuma faixa ativa"})
            self.store.step(1)
            await self._ipc(["playlist-next"])
            return _json(200, {"ok": True, "index": self.store.index})
        if method == "POST" and path in ("/api/v1/prev", "/api/v1/previous"):
            if not await self._engine_has_track():
                await self.spy.event("api_engine_idle", path=path)
                return _json(503, {"error": "engine-idle",
                                   "detail": "motor ocioso — nenhuma faixa ativa"})
            self.store.step(-1)
            await self._ipc(["playlist-prev"])
            return _json(200, {"ok": True, "index": self.store.index})
        if method == "POST" and path == "/api/v1/stop":
            return await self._one_cmd("stop")
        if method == "POST" and path == "/api/v1/seek":
            return await self._seek(payload)
        if method == "POST" and path == "/api/v1/shuffle":
            return await self._shuffle(payload)
        if method == "POST" and path == "/api/v1/repeat":
            return await self._repeat(payload)
        if method == "POST" and path == "/api/v1/volume":
            return await self._volume(payload)
        if method == "POST" and path == "/api/v1/spy":
            return await self._spy_beacon(payload)
        return None

    # --------------------------------------------------------------- handlers
    async def _health(self):
        return _json(
            200,
            {
                "ok": True,
                "svc": "navidrome-remote",
                "version": SERVICE_VERSION,
                "navidrome_anchor": NAVIDROME_ANCHOR,
                "phase": PHASE,
                "gate": "session-3layer" + ("-static" if self.cfg.api_token else ""),
                "engine": self._engine_state(),
            },
        )

    async def _status(self):
        """Rich playback state (shapes in the module docstring).

        Engine-down is reported gracefully: play=null, volume=null,
        playing=false. A `media-title` that is ITSELF a URL is sanitized
        to null before crossing to any client (invariant: no stream URL
        in any response).
        """
        # NOTE: `alive` is a PROPERTY — it must be accessed inside the
        # worker thread (a lambda), or it evaluates eagerly on the event
        # loop (blocking it) and to_thread would receive a bool.
        try:
            alive = await asyncio.to_thread(lambda: self.engine.alive)
        except Exception:
            alive = False
        if not alive:
            return _json(200, {
                "phase": PHASE, "engine": "down", "idle": True,
                "play": None, "playing": False, "paused": False,
                "shuffle": False,
                "repeat": self.store.settings.get("repeat", "off"),
                "volume": None,
                "queue": self._queue_view(False),
            })
        props = await asyncio.to_thread(self.engine.get_properties, [
            "idle", "playback-status", "time-pos", "media-title",
            "shuffle", "loop-file", "loop-playlist", "volume",
        ])
        idle = props.get("idle")
        idle = True if idle is None else bool(idle)
        paused = (props.get("playback-status") == "paused")
        playing = (not idle) and (not paused)
        vol = props.get("volume")
        try:
            vol_i = int(round(float(vol)))
        except (TypeError, ValueError):
            vol_i = None
        return _json(200, {
            "phase": PHASE,
            "engine": "connected",
            "idle": idle,
            "play": self._describe_current(
                props, active=(playing or paused)),
            "playing": playing,
            "paused": paused,
            "shuffle": bool(int(props.get("shuffle") or 0)),
            "repeat": self._repeat_from_loops(
                props.get("loop-file"), props.get("loop-playlist")),
            "volume": vol_i,
            "queue": self._queue_view(False),
        })

    def _describe_current(self, props: dict, active: bool) -> "dict | None":
        if not active:
            return None
        title = props.get("media-title")
        if isinstance(title, str) and title.startswith(("http://", "https://")):
            title = None  # a URL wearing the costume of a title: sanitized
        pid = None
        items = self.store.items
        idx = self.store.index
        if items and isinstance(idx, int) and 0 <= idx < len(items):
            cur = items[idx]
            title = cur.title or title
            pid = cur.id
        pos = props.get("time-pos")
        try:
            pos = float(pos) if pos is not None else None
        except (TypeError, ValueError):
            pos = None
        return {"id": pid, "title": title, "pos": pos}

    async def _queue_get(self):
        view = self._queue_view(with_items=True)
        settings = self.store.settings_snapshot()
        settings["index"] = self.store.index
        view["settings"] = settings
        view["engine"] = self._engine_state()
        return _json(200, view)

    def _queue_view(self, with_items: bool) -> dict:
        if not with_items:
            return {"length": len(self.store.items), "index": self.store.index}
        return self.store.safe_snapshot()

    async def _queue_post(self, payload: dict):
        items = payload.get("items")
        if items is None and isinstance(payload.get("id"), str):
            items = [payload]
        if not isinstance(items, list) or not items:
            return _json(400, {"error": "precisa-de-'items'(lista não vazia)"})
        if len(items) > MAX_ITEMS:
            return _json(400, {"error": "máximo de %d itens" % MAX_ITEMS})
        mode = str(payload.get("mode", "replace")).lower()
        if mode not in ("replace", "append"):
            return _json(400, {"error": "'mode' deve ser replace|append"})
        try:
            if mode == "replace":
                self.store.replace(items)
            else:
                self.store.append(items)
        except StoreError as exc:
            await self.spy.event("api_store_rejected", path="/api/v1/queue", error=str(exc))
            return _json(400, {"error": str(exc)})

        if not (self.cfg.nav_user and self.cfg.nav_pass):
            return _json(501, {"error": "credentials-missing",
                               "detail": "NVRA_NAV_USER/NVRA_NAV_PASS vazios — a fila foi registrada mas a aplicação ao motor espera as credenciais"})

        from .queue import apply_to_engine
        ok = await apply_to_engine(self.store, self.engine, self.spy)
        if not ok:
            # engine down NOW: keep the intent PENDING so the lazy
            # restore loop applies it the moment the engine comes up.
            self.store.pending_restore = True
            return _json(503, {"error": "engine-down",
                               "detail": "fila persistida; aplicação ao motor será retomada quando o motor subir"})
        return _json(200, {"ok": True, "mode": mode,
                           "length": len(self.store.items),
                           "index": self.store.index})

    async def _play(self, payload: dict):
        url = payload.get("url")
        kind = "url"
        title = payload.get("title")
        song_id = payload.get("id")
        if not url:
            if not song_id:
                return _json(400, {"error": "precisa-de-'id'-ou-'url'"})
            kind = "id"
            if not (self.cfg.nav_user and self.cfg.nav_pass):
                return _json(
                    501,
                    {"error": "credentials-missing",
                     "detail": "NVRA_NAV_USER/NVRA_NAV_PASS vazios"},
                )
            url = subsonic_stream_url(
                self.cfg.upstream_base_url, str(song_id),
                self.cfg.nav_user, self.cfg.nav_pass,
            )
        await self._ipc(["loadfile", str(url), "replace"])
        if kind == "id":
            self.store.note_replacement({"id": str(song_id), "title": title})
        else:
            self.store.note_replacement({})
        # INVARIANT: the composed URL stays server-side — and so does
        # anything derived from it. The client learns only WHAT it asked
        # for (kind). Titles it provided are not echoed back either
        # (smallest lawful surface).
        return _json(200, {"ok": True, "kind": kind})

    async def _toggle_pause(self, value: bool):
        await self._ipc(["set_property", "pause", bool(value)])
        return _json(200, {"ok": True, "pause": bool(value)})

    async def _one_cmd(self, name: str):
        await self._ipc([name])
        return _json(200, {"ok": True, "cmd": name})

    async def _seek(self, payload: dict):
        pos = payload.get("pos", payload.get("position"))
        if pos is None:
            return _json(400, {"error": "precisa-de-'pos'-(segundos)"})
        try:
            pos = float(pos)
        except (TypeError, ValueError):
            return _json(400, {"error": "'pos' inválido"})
        if pos < 0:
            return _json(400, {"error": "'pos' negativo"})
        await self._ipc(["set_property", "time-pos", pos])
        return _json(200, {"ok": True, "pos": pos})

    async def _shuffle(self, payload: dict):
        val = payload.get("value")
        if val is None:
            cur = await self._prop_or_zero("shuffle")
            val = int(not cur)
        val = int(bool(val))
        await self._ipc(["set_property", "shuffle", val])
        self.store.touch_settings(shuffle=bool(val))
        return _json(200, {"ok": True, "enabled": bool(val)})

    async def _repeat(self, payload: dict):
        mode_raw = str(payload.get("mode", "off")).lower()
        spec = REPEAT_MODES.get(mode_raw)
        if spec is None:
            return _json(400, {"error": "'mode' inválido",
                               "valid": ["off", "track", "playlist"]})
        norm, loop_file, loop_pl = spec
        await self._ipc(["set_property", "loop-file", loop_file])
        await self._ipc(["set_property", "loop-playlist", loop_pl])
        self.store.touch_settings(repeat=norm)
        return _json(200, {"ok": True, "mode": norm})

    async def _volume(self, payload: dict):
        level = payload.get("level", payload.get("volume"))
        if level is None:
            return _json(400, {"error": "precisa-de-'level'-0a100"})
        try:
            level = float(level)
        except (TypeError, ValueError):
            return _json(400, {"error": "'level' inválido"})
        if not 0 <= level <= 100:
            return _json(400, {"error": "'level' fora de 0..100"})
        await self._ipc(["set_property", "volume", level])
        self.store.touch_settings(volume=int(level))
        return _json(200, {"ok": True, "level": int(level)})

    async def _spy_beacon(self, payload: dict):
        """Client-side trace landing pad for bridge.js. GATED (M1): the
        beacon must carry the page session (`jwt` in the body) — the gate
        extracts it from the payload."""
        kind = str(payload.get("k", "unknown"))[:64]
        detail = str(payload.get("d", ""))[:512]
        await self.spy.event("bridge", beacon=kind, detail=detail)
        return _json(200, {"ok": True, "logged": True})

    # -------------------------------------------------------------- internals
    def _engine_state(self) -> str:
        return "connected" if self.engine.alive else "down"

    async def _engine_has_track(self) -> bool:
        """Alive AND serving a track (idle engines answer next/prev with
        503 by contract — a kiosk command cannot ride a stopped engine)."""
        try:
            if not self.engine.alive:
                return False
        except Exception:
            return False
        try:
            props = await asyncio.to_thread(self.engine.get_properties,
                                            ["idle", "playlist-size"])
        except (EngineDown, RuntimeError):
            return False
        idle = props.get("idle")
        idle = True if idle is None else bool(idle)
        try:
            ps = int(props.get("playlist-size") or 0)
        except (TypeError, ValueError):
            ps = 0
        return (not idle) and (ps > 0)

    def _repeat_from_loops(self, loop_file, loop_pl) -> str:
        try:
            lf = int(loop_file or 0)
            lp = int(loop_pl or 0)
        except (TypeError, ValueError):
            return "off"
        if lf < 0:
            return "track"
        if lp < 0:
            return "playlist"
        return "off"

    async def _prop_or_zero(self, name: str):
        try:
            return await asyncio.to_thread(self.engine.get_property, name)
        except (EngineDown, RuntimeError):
            return 0

    async def _ipc(self, cmd_args) -> None:
        """Deterministic engine call (see M0' docs)."""
        await asyncio.to_thread(self.engine.cmd, *cmd_args)
