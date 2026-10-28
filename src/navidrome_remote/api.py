"""The SBC control API: /api/v1/* (spec v1.2, componente B).

M0' scope ("stub funcional" — the addendum's letter): the routes exist,
validate their payloads, drive the playback engine over JSON-IPC, and report
structured JSON status/errors. What M0' deliberately leaves out (documented
so reviewers don't mistake omission for oversight):

  * session gate — M1: an active Navidrome session (cookie observed by the
    proxy) will gate these routes; today only the optional static bearer
    token (NVRA_API_TOKEN) applies;
  * persistent queue/state — M1;
  * the definitive bridge.js interceptors — M1 (the M0' bridge only beacons
    boot/error traces to /api/v1/spy).

Subsonic stream URLs (when `play` is given a song `id`) are constructed with
the SERVICE ACCOUNT from the EnvironmentFile — credentials never reach the
browser (invariante de credenciais), and mpv consumes the URL directly
(no transcode).
"""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import urlencode

from . import NAVIDROME_ANCHOR, SERVICE_VERSION, SUBSONIC_CLIENT_VER
from .ipc import EngineDown
from .spy import Spy


def _json(status: int, payload: dict) -> "tuple[int, str, bytes]":
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return status, "application/json; charset=utf-8", body


def subsonic_stream_url(upstream_base: str, song_id: str, user: str, password: str) -> str:
    """Navidrome/Subsonic `stream` URL for a song id.

    Salt = first 12 chars of base64(password) (Subsonic spec).
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


class Api:
    AVAILABLE = (
        "GET /api/v1/health",
        "GET /api/v1/status",
        "POST /api/v1/play",
        "POST /api/v1/pause",
        "POST /api/v1/resume",
        "POST /api/v1/next",
        "POST /api/v1/previous",
        "POST /api/v1/stop",
        "POST /api/v1/seek",
        "POST /api/v1/volume",
        "POST /api/v1/spy",
    )

    def __init__(self, cfg, spy: Spy, engine):
        self.cfg = cfg
        self.spy = spy
        self.engine = engine

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

        if self.cfg.api_token:
            auth = (headers.get_first("authorization") or "").strip().lower()
            if auth != f"bearer {self.cfg.api_token}".lower():
                await self.spy.event("api_denied", path=path, reason="token")
                return _json(401, {"error": "unauthorized"})

        try:
            resp = await self._dispatch(method, path, payload)
            if resp is not None:
                return resp
        except EngineDown as exc:
            await self.spy.event("api_engine_down", path=path, error=str(exc))
            return _json(
                503,
                {
                    "error": "engine-down",
                    "detail": "socket IPC inacessível — unidade mpv (ou mock) não está no ar",
                },
            )
        except RuntimeError as exc:
            await self.spy.event("api_rejected", path=path, error=str(exc))
            return _json(502, {"error": "engine-rejected", "detail": str(exc)})

        await self.spy.event("api_unknown_route", method=method, path=path)
        return _json(404, {"error": "unknown-endpoint", "available": list(self.AVAILABLE)})

    async def _dispatch(self, method: str, path: str, payload: dict) -> "tuple[int, str, bytes] | None":
        if method == "GET" and path == "/api/v1/health":
            return await self._health()
        if method == "GET" and path == "/api/v1/status":
            return await self._status()
        if method == "POST" and path == "/api/v1/play":
            return await self._play(payload)
        if method == "POST" and path == "/api/v1/pause":
            return await self._toggle_pause(True)
        if method == "POST" and path == "/api/v1/resume":
            return await self._toggle_pause(False)
        if method == "POST" and path == "/api/v1/next":
            return await self._one_cmd("playlist-next")
        if method == "POST" and path == "/api/v1/previous":
            return await self._one_cmd("playlist-prev")
        if method == "POST" and path == "/api/v1/stop":
            return await self._one_cmd("stop")
        if method == "POST" and path == "/api/v1/seek":
            return await self._seek(payload)
        if method == "POST" and path == "/api/v1/volume":
            return await self._volume(payload)
        if method == "POST" and path == "/api/v1/spy":
            return await self._spy_beacon(payload)
        return None

    # --------------------------------------------------------------- handlers
    async def _health(self) -> "tuple[int, str, bytes]":
        return _json(
            200,
            {
                "ok": True,
                "svc": "navidrome-remote",
                "version": SERVICE_VERSION,
                "navidrome_anchor": NAVIDROME_ANCHOR,
                "phase": "M0'-stub",
                "engine": self._engine_state(),
            },
        )

    async def _status(self) -> "tuple[int, str, bytes]":
        try:
            props = await asyncio.to_thread(
                self.engine.get_properties, ["idle", "playback-status", "time-pos", "media-title"]
            )
        except EngineDown:
            return _json(200, {"engine": "down"})
        paused = props.get("playback-status") == "paused"
        # conservative default: a MISSING idle value (odd engine build)
        # reads as idle, never as playing.
        idle_val = props.get("idle")
        idle = True if idle_val is None else bool(idle_val)
        return _json(
            200,
            {
                "engine": "connected",
                "idle": idle,
                "paused": bool(paused),
                "playing": (not idle) and (not paused),
                "time_pos": props.get("time-pos"),
                "media_title": props.get("media-title"),
            },
        )

    async def _play(self, payload: dict) -> "tuple[int, str, bytes]":
        url = payload.get("url")
        if not url:
            song_id = payload.get("id")
            if not song_id:
                return _json(400, {"error": "precisa-de-'id'-ou-'url'"})
            if not (self.cfg.nav_user and self.cfg.nav_pass):
                return _json(
                    501,
                    {
                        "error": "sem-credenciais-navidrome",
                        "detail": "NVRA_NAV_USER/NVRA_NAV_PASS vazios — M0' aceita url explícita",
                    },
                )
            url = subsonic_stream_url(
                self.cfg.upstream_base_url, str(song_id), self.cfg.nav_user, self.cfg.nav_pass
            )
        await self._ipc(["loadfile", str(url), "replace"])
        shown = str(url)
        return _json(
            200,
            {
                "accepted": True,
                "cmd": "loadfile",
                "file_set": shown if len(shown) <= 96 else shown[:93] + "...",
            },
        )

    async def _toggle_pause(self, value: bool) -> "tuple[int, str, bytes]":
        await self._ipc(["set_property", "pause", bool(value)])
        return _json(200, {"accepted": True, "pause": bool(value)})

    async def _one_cmd(self, name: str) -> "tuple[int, str, bytes]":
        await self._ipc([name])
        return _json(200, {"accepted": True, "cmd": name})

    async def _seek(self, payload: dict) -> "tuple[int, str, bytes]":
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
        return _json(200, {"accepted": True, "pos": pos})

    async def _volume(self, payload: dict) -> "tuple[int, str, bytes]":
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
        return _json(200, {"accepted": True, "level": level})

    async def _spy_beacon(self, payload: dict) -> "tuple[int, str, bytes]":
        """Client-side trace landing pad for bridge.js (M0' boot evidence)."""
        kind = str(payload.get("k", "unknown"))[:64]
        detail = str(payload.get("d", ""))[:512]
        await self.spy.event("bridge", beacon=kind, detail=detail)
        return _json(200, {"logged": True})

    # -------------------------------------------------------------- internals
    def _engine_state(self) -> str:
        return "connected" if self.engine.alive else "down"

    async def _ipc(self, cmd_args) -> None:
        """Deterministic engine call: awaits the engine's ack via to_thread
        (mpv JSON-IPC is datagram; a response always arrives on success or
        rejection)."""
        await asyncio.to_thread(self.engine.cmd, *cmd_args)
