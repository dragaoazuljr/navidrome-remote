"""The SBC control API: /api/v1/* (spec v1.2, componente B — M1/A3).

Routes (complete contract, card A3)
-----------------------------------
  GET  /api/v1/health    PUBLIC liveness probe (never gated).
  GET  /api/v1/status    playback state + queue/settings view.
  POST /api/v1/play      play a song id (server-built stream URL) or an
                         explicit url (M0' compat; see design doc).
  POST /api/v1/pause     pause (recorded: a restart re-applies the
                         paused picture — A5).
  POST /api/v1/resume    unpause; A5: ALSO starts a PARKED list (an
                         idle engine holding a non-empty queue) — an
                         idle engine obeys no implicit `pause false`;
                         graceful no-op when there is nothing to resume.
  POST /api/v1/next      playlist-next.
  POST /api/v1/prev      playlist-prev (`previous` accepted as alias).
  POST /api/v1/stop      stop (recorded: a restart loads the list
                         PARKED — zero unexpected audio after boot).
  POST /api/v1/seek      {"pos": seconds}.
  POST /api/v1/shuffle   {"value": bool} or bare call = toggle.
  POST /api/v1/repeat    {"mode": off|track|playlist} (aliases: all, one).
  POST /api/v1/volume    {"level": 0..100}.
  GET  /api/v1/queue     sanitized queue view (id/title — no URLs).

Concrete shapes (M1 contract)
-----------------------------
  GET /status -> {phase, engine:"connected"|"down", idle,
                  play:{id,title,pos,dur}|null, playing, paused,
                  shuffle:boolean, repeat:"off"|"track"|"playlist",
                  volume:int|null, queue:{length,index,size}}
        (A4 additive fields: `play.dur` — loaded duration; `queue.size`
        — the ENGINE's playlist count, so a UI can compare intent vs
        engine at a glance. Null when unavailable.)
  GET /queue  -> {items:[{id,title?}...], settings:{index,volume,
                  shuffle,repeat}, engine}
  next/prev while the engine is IDLE (no active track) -> 503
  engine-idle (a kiosk command cannot ride a stopped engine); a DEAD
  engine is reported as engine-down.
  POST /api/v1/queue     {"items":[{id,title?}...], "mode": replace|append}.
  POST /api/v1/spy       client-side beacon (bridge.js); carries the
                         session `jwt` in the body (sendBeacon cannot set
                         headers — see gateway.L1).

Translation (A4 — /api/v1/* -> stock mpv JSON-IPC)
-------------------------------------------------
  The routes validate input HERE, then delegate to
  controller.PlaybackController, which owns one deterministic command
  per action:
    play            loadfile <url> "replace"
    pause / resume  set_property pause true/false
    stop            stop
    next / prev     playlist-next / playlist-prev
    seek {pos}      seek <effective> "absolute"   (clamped to duration)
    shuffle {v}     set_property shuffle 0/1     (engine reorders on 0->1)
    repeat {m}      set_property loop-file / loop-playlist pair
    volume {lvl}    set_property volume <float>
    queue (POST)    playlist-clear; playlist-add <url> <i> <id> per entry;
                    playlist-play <start>; mirrored settings
    status (GET)    get_properties (observation set) + engine->store
                    reconciliation on `playlist-current-id`
  The next/prev RESPONSE index is the store's stepped value (optimistic
  while shuffling); the FOLLOWING /status call carries the engine-
  reconciled index — eventual per request, strong via polling.

  DOUBLE-INTENT ABSORPTION (A6): one physical gesture from the UI plane
  arrives as a PAIR of writes — the media-source swap (/play {id}) and
  the transport button (/next|/prev), in either order within
  `TRANSPORT_ECHO_MS`. The pair commutes via three rules:
    * /play {id == current}      -> 200 echo, no write (re-selecting
                                     the current track is not a
                                     selection; a real restart is
                                     `seek 0`). Keeps a seeded deck
                                     intact;
    * /play adjacent (±1)        -> the write is HELD for
                                     `PLAY_DEFER_S`; a matching
                                     /next|/prev in the window cancels
                                     it ("the step is the primary
                                     intent") or, if the play already
                                     landed, the step folds to a no-op
                                     200; on the hold deadline the play
                                     executes as an ordinary structural
                                     play;
    * /next|/prev with a recent  /play targeting this step's own
                                     destination -> same fold.
  Any unmatched /play keeps the A3 contract EXACTLY (structural
  replacement via loadfile "replace"). Absorption events land in the
  spy (`a6_double_intent_absorbed` / `a6_same_id_play_absorbed`).

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
import time
from urllib.parse import urlencode

from . import NAVIDROME_ANCHOR, SERVICE_VERSION, SUBSONIC_CLIENT_VER
from .controller import PlaybackController
from .ipc import EngineDown
from .queue import MAX_ITEMS, QueueStore, StoreError
from .spy import Spy

PHASE = "M1"

# A6 — double-intent absorption. The browser plane expresses ONE gesture
# (a next/prev press) as TWO writes: the UI swaps the media source
# (bridge /play {id}) and the button (bridge /next|/prev), often within
# milliseconds and in EITHER order. Unfolded, that pair corrupts the
# deck (structural replacement + a step against a single-entry
# playlist = engine stops). The absorbers below make the pair
# commute: whichever member arrives first, the engine ends on the
# position the single gesture denoted, and a multi-item deck survives.
TRANSPORT_ECHO_MS = 1000  # window in which two writes are "one gesture"
PLAY_DEFER_S = 0.40       # hold for an adjacent /play awaiting its twin


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


# A4: alias -> normalized mode. The (loop-file, loop-playlist) EXECUTION
# pair lives in controller.REPEAT_PLAN — one definition, one translator.
REPEAT_ALIASES = {
    "off": "off",
    "track": "track",
    "playlist": "playlist",
    "all": "playlist",      # Navidrome UI spelling
    "one": "track",
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
        self.controller = PlaybackController(cfg, spy, engine, self.store)
        # Double-intent ledger (A6): monotonic stamps of the last
        # /next and /prev, plus the last /play intent ({t, frm, to,
        # gate}); `gate` is a pending deferral future while the /play
        # holds before deciding to write.
        self._tx = {"next": 0.0, "prev": 0.0, "play": None}
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
            # A5 — POLICY (records are facts): persist the "paused"
            # phase ONLY when the engine is actively presenting a track
            # (alive, not idle, non-empty list). A pause on an IDLE
            # engine changes nothing there, so there is no phase to
            # record — and a phantom "paused" would be worse than none
            # (a restart would try to resurrect a pause that never
            # existed). The TRANSPORT always emits its one stock
            # command (the A4 contract lives in controller.pause); a
            # dead engine still 503s through `handle()` below.
            presenting = await self._engine_presenting()
            await asyncio.to_thread(self.controller.pause, True)
            if presenting:
                sp = getattr(self.store, "set_phase", None)
                if sp is not None:
                    sp("paused", force=True)  # discrete tx — sync flush
            return _json(200, {"ok": True, "pause": True})
        if method == "POST" and path == "/api/v1/resume":
            return await self._resume()
        if method == "POST" and path in ("/api/v1/next",):
            if not await self._engine_has_track():
                await self.spy.event("api_engine_idle", path=path)
                return _json(503, {"error": "engine-idle",
                                   "detail": "motor ocioso — nenhuma faixa ativa"})
            verdict = self._absorb_adjacent_play(time.monotonic(), +1)
            if verdict == "absorbed":
                await self.spy.event("a6_double_intent_absorbed",
                                     path=path, mode="stepped-play")
                return _json(200, {"ok": True, "index": self.store.index,
                                   "echo": "adjacent-play"})
            self._tx["next"] = time.monotonic()
            self.store.step(1)
            await asyncio.to_thread(self.controller.next)
            return _json(200, {"ok": True, "index": self.store.index})
        if method == "POST" and path in ("/api/v1/prev", "/api/v1/previous"):
            if not await self._engine_has_track():
                await self.spy.event("api_engine_idle", path=path)
                return _json(503, {"error": "engine-idle",
                                   "detail": "motor ocioso — nenhuma faixa ativa"})
            verdict = self._absorb_adjacent_play(time.monotonic(), -1)
            if verdict == "absorbed":
                await self.spy.event("a6_double_intent_absorbed",
                                     path=path, mode="stepped-play")
                return _json(200, {"ok": True, "index": self.store.index,
                                   "echo": "adjacent-play"})
            self._tx["prev"] = time.monotonic()
            self.store.step(-1)
            await asyncio.to_thread(self.controller.prev)
            return _json(200, {"ok": True, "index": self.store.index})
        if method == "POST" and path == "/api/v1/stop":
            await asyncio.to_thread(self.controller.stop)
            sp = getattr(self.store, "set_phase", None)
            if sp is not None:
                # A5: stopping is DELIBERATE silence — record it (the
                # position anchor is wiped inside set_phase). On a
                # service restart the list loads PARKED: zero unexpected
                # audio after boot (ADR-004).
                sp("stopped", force=True)
            return _json(200, {"ok": True, "cmd": "stop"})
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
        # A4: batched observation + engine->store reconciliation live
        # in the controller; the response shape stays the M1 contract,
        # plus the additive `play.dur` / `queue.size` fields.
        props = await asyncio.to_thread(self.controller.snapshot)
        idle = props.get("idle")
        idle = True if idle is None else bool(idle)
        paused = (props.get("playback-status") == "paused")
        playing = (not idle) and (not paused)
        vol = props.get("volume")
        try:
            vol_i = int(round(float(vol)))
        except (TypeError, ValueError):
            vol_i = None
        try:
            qsize = int(props.get("playlist-count") or 0) or None
        except (TypeError, ValueError):
            qsize = None
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
            "queue": self._queue_view(False, engine_size=qsize),
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
        # A4: the engine is the source of truth while a track is
        # active — its current id (the song id the controller tags on
        # every playlist entry) outranks the store's pointer, which
        # can lag a shuffle.
        cid = props.get("playlist-current-id")
        hit = None
        if isinstance(cid, str) and cid:
            hit = self.store.find_by_id(cid)
        if hit is None:
            hit = idx if isinstance(idx, int) else None
        if items and isinstance(hit, int) and 0 <= hit < len(items):
            cur = items[hit]
            title = cur.title or title
            pid = cur.id
        pos = props.get("time-pos")
        try:
            pos = float(pos) if pos is not None else None
        except (TypeError, ValueError):
            pos = None
        dur = props.get("duration")
        try:
            dur = float(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        return {"id": pid, "title": title, "pos": pos, "dur": dur}

    async def _queue_get(self):
        view = self._queue_view(with_items=True)
        settings = self.store.settings_snapshot()
        settings["index"] = self.store.index
        view["settings"] = settings
        view["engine"] = self._engine_state()
        # A5 — additive transparency of the persisted picture (plain
        # scalars only, nothing secret crosses): the TRANSPORT PHASE a
        # restart will re-apply, the last observed position, and the
        # monotonic revision counter (proof the record ADVANCES under
        # concurrent writers — zero lost updates, ADR-004).
        view["phase"] = getattr(self.store, "phase", "playing")
        view["pos"] = getattr(self.store, "pos", None)
        view["seq"] = getattr(self.store, "seq", 0)
        return _json(200, view)

    def _queue_view(self, with_items: bool,
                    engine_size: "int | None" = None) -> dict:
        if not with_items:
            return {"length": len(self.store.items),
                    "index": self.store.index, "size": engine_size}
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

        if mode == "replace":
            sp = getattr(self.store, "set_phase", None)
            if sp is not None:
                # ADR-004 (A5): replacing the queue is an explicit INTENT
                # to listen — the transport phase is set to playing
                # whether or not the engine can start it RIGHT NOW (the
                # 503 pending path below keeps the intent PENDING so the
                # restore loop ships it when the engine comes up; a
                # service restart finds the same intention). `append`
                # deliberately leaves the phase untouched: stacking
                # entries onto a PAUSED list does not make the user
                # listen.
                sp("playing", force=True)

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

    def _absorb_adjacent_play(self, now: float, delta: int) -> str | None:
        """Double-intent absorption for this step (A6).

        If the LAST /play arrived inside `TRANSPORT_ECHO_MS` and aimed at
        exactly the position THIS step would step to (its recorded
        from->to delta equals `delta`), that /play is the UI's half of
        the same gesture:

        * still in deferral -> its gate is cancelled (the structural
          replacement never happens) and the STEP proceeds against the
          intact deck;
        * already executed -> it structurally replaced the queue with
          the very target of this step, so stepping again would
          overshoot (or stop at a single-entry playlist end); the step
          folds into a no-op 200.

        Returns "cancel" (step proceeds), "absorbed" (caller returns
        200 without stepping) or None (no relation — step normally).
        """
        pp = self._tx.get("play")
        if pp is None or pp.get("to") is None or pp.get("frm") is None:
            return None
        if pp["to"] - pp["frm"] != delta:
            return None
        if (now - pp["t"]) > TRANSPORT_ECHO_MS / 1000.0:
            return None
        gate = pp.get("gate")
        if gate is not None and not gate.done():
            gate.set_result(str(delta))  # pending /play stands down
            return "cancel"
        return "absorbed"

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
        if kind == "id":
            idx = self.store.find_by_id(str(song_id))
            cur = self.store.index
            cur = cur if isinstance(cur, int) else None
            now = time.monotonic()
            if idx is not None and idx == cur:
                # Re-selecting the current track is an echo, not a
                # selection: honoring it as a structural replacement
                # would flatten a multi-item deck for no gain (a real
                # restart is `seek 0`). Deck survives; nothing reloads.
                self._tx["play"] = {"t": now, "frm": cur, "to": idx,
                                    "gate": None}
                await self.spy.event("a6_same_id_play_absorbed",
                                     path="/api/v1/play")
                return _json(200, {"ok": True, "kind": kind,
                                   "echo": "same-id"})
            delta = (idx - cur) if (idx is not None and cur is not None) \
                else None
            if delta in (1, -1):
                opp = "next" if delta == 1 else "prev"
                if (now - self._tx[opp]) <= TRANSPORT_ECHO_MS / 1000.0:
                    # The twin transport already STEPPED us to this
                    # exact id: the /play is its echo — no write.
                    self._tx["play"] = {"t": now, "frm": cur, "to": idx,
                                        "gate": None}
                    await self.spy.event("a6_double_intent_absorbed",
                                         path="/api/v1/play",
                                         mode="echo-of-" + opp)
                    return _json(200, {"ok": True, "kind": kind,
                                       "echo": opp + "-echo"})
                # Twin not here YET: an adjacent hop this soon after a
                # deck operation is overwhelmingly the UI's half of an
                # inbound next/prev. Hold the write briefly; a matching
                # step arriving inside the window cancels this (its
                # absorber wakes the gate). Ordinary selections of a
                # neighbor song pay at most `PLAY_DEFER_S`.
                gate = asyncio.get_running_loop().create_future()
                self._tx["play"] = {"t": now, "frm": cur, "to": idx,
                                    "gate": gate}
                try:
                    await asyncio.wait_for(gate, timeout=PLAY_DEFER_S)
                    await self.spy.event("a6_double_intent_absorbed",
                                         path="/api/v1/play",
                                         mode="deferred-standing-down")
                    return _json(200, {"ok": True, "kind": kind,
                                       "echo": "superseded-by-" +
                                               str(gate.result())})
                except asyncio.TimeoutError:
                    gate = None  # deadline: execute as an ordinary play
                    self._tx["play"] = {"t": now, "frm": cur, "to": idx,
                                        "gate": None}
            else:
                self._tx["play"] = {"t": now, "frm": cur, "to": idx,
                                    "gate": None}
        await asyncio.to_thread(self.controller.play, url)
        if kind == "id":
            self.store.note_replacement({"id": str(song_id), "title": title})
        else:
            self.store.note_replacement({})
        # A6.2 — POLICY paired with A5 (ADR-004, "records are facts"):
        # the engine's ack of a fulfilled explicit /play SATISFIES the
        # pending selection the request raised (the restore loop must
        # not re-apply a picture afterwards). And the intent IS a
        # listen intent: record it as the transport phase, because the
        # stored default at boot is "stopped" and any later re-apply
        # (the 5 s loop, a reconnect, a restart) re-applies the stored
        # image verbatim — forensics: store_apply_parked 1.9 s after
        # /play parked the engine mid-session and ate the user's next
        # /pause (T3). An UNFULFILLED intent never reaches this point
        # (to_thread raises first), so it stays pending as before.
        sp = getattr(self.store, "set_phase", None)
        if sp is not None:
            sp("playing", force=True)
        self.store.pending_restore = False
        # F7b — a fulfilled explicit /play also RESOLVES any loaded
        # restore window: the user's own intent supersedes it.
        if getattr(self.store, "restore_armed", False):
            self.store.restore_armed = False
        # INVARIANT: the composed URL stays server-side — and so does
        # anything derived from it. The client learns only WHAT it asked
        # for (kind). Titles it provided are not echoed back either
        # (smallest lawful surface).
        return _json(200, {"ok": True, "kind": kind})

    async def _engine_presenting(self) -> bool:
        """A5 — "is the engine actively presenting a track right now"
        (alive, not idle, non-empty list — the controller's
        active_with_track, off the event loop). Down engines abort the
        probe gracefully here (False) — the SUBSEQUENT transport call
        is what turns an engine-down into the standard 503 via
        `handle()`, keeping one owner for each signal."""
        try:
            return await asyncio.to_thread(
                self.controller.active_with_track)
        except (RuntimeError, Exception):
            return False

    async def _resume(self):
        """A5 — the phase-aware resume (full contract in
        `controller.MPVPlaybackController.resume`).

        The engine may be presenting (a) a paused FILE (lift the
        pause), (b) a PARKED LIST — an idle engine holding a non-empty
        queue, the classic trap: a plain `pause false` would leave it
        SILENT forever (an idle engine obeys neither an implicit
        unpause nor a hidden autoplay) — so /resume STARTS it at the
        persisted index, or (c) nothing at all (graceful no-op: a ghost
        request must never 500 the user). The store's phase is aligned
        by the controller with what actually happened."""
        try:
            action = await asyncio.to_thread(self.controller.resume)
        except Exception as exc:
            await self.spy.event("api_resume_failed",
                                 error=type(exc).__name__)
            return _json(503, {"error": "engine-down",
                               "detail": "motor indisponível — nada foi alterado"})
        await self.spy.event("api_resumed", action=action)
        return _json(200, {"ok": True, "pause": False, "action": action})

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
        eff = await asyncio.to_thread(self.controller.seek, pos)
        return _json(200, {"ok": True, "pos": eff})

    async def _shuffle(self, payload: dict):
        val = payload.get("value")
        if val is None:
            cur = await self._prop_or_zero("shuffle")
            val = int(not cur)
        val = int(bool(val))
        await asyncio.to_thread(self.controller.shuffle, val)
        self.store.touch_settings(shuffle=bool(val))
        return _json(200, {"ok": True, "enabled": bool(val)})

    async def _repeat(self, payload: dict):
        mode_raw = str(payload.get("mode", "off")).lower()
        norm = REPEAT_ALIASES.get(mode_raw)
        if norm is None:
            return _json(400, {"error": "'mode' inválido",
                               "valid": ["off", "track", "playlist"]})
        await asyncio.to_thread(self.controller.repeat, norm)
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
        await asyncio.to_thread(self.controller.volume, level)
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
        503 by contract — a kiosk command cannot ride a stopped
        engine). A4: the check itself (alive + not-idle +
        playlist-count > 0) lives in the controller; this wrapper
        keeps it off the event loop."""
        return await asyncio.to_thread(self.controller.active_with_track)

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

