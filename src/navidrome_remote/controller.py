"""mpv playback controller — the logical driver above the JSON-IPC transport (A4).

Layering
--------
  Api  (presentation: /api/v1/* validation + response shapes — M1 contract)
     |
  PlaybackController  (THIS MODULE: semantics, protocol, reconciliation)
     |
  MpvEngine (ipc.py: framed JSON-NL over AF_UNIX datagram, request ids)
     |
  engine: real mpv (2W/C3) or tools/fake_mpv_sink.py (C1) — same socket.

Responsibilities (card A4 acceptance)
-------------------------------------
1. QUITA — the Navidrome-side queue (ids/titles in `QueueStore`) is driven
   into the engine's playlist, entry-for-entry identifiable: every engine
   playlist entry carries the Navidrome song `id` as its mpv `id` field,
   so the engine can be asked "WHAT is playing?" (`playlist-current-id`)
   and the answer maps unambiguously back to the queue (both directions:
   queue -> engine, engine -> queue).

2. TRANSPORTE — next/prev/seek/stop/pause/volume/shuffle/repeat expressed
   as the STOCK mpv primitives (one IPC command each; see TRANSLATION in
   the module docstring of api.py). Seek goes out as `seek <t> absolute`,
   clamped to the loaded duration when the engine reports one.

3. ESTADO — `snapshot()` reads the observation set (one batched round)
   and `reconcile()` makes the two state holders agree: while a track is
   ACTIVE the engine is the source of truth (store.index follows
   `playlist-current-id` — immune to shuffle reorderings; persisted, so a
   service restart never regresses it); when nothing plays the store keeps
   holding the intent (A3 semantics, reboot recovery).

4. GAPLESS (best-effort — spec v1.2, quality target of 2W)
   * the FULL queue is loaded at apply time: the demuxer prefetches the
     next entry — no mid-playlist loadfile storms, no idle windows;
   * transport transitions use `playlist-next`/`playlist-prev` (the native
     hand-off inside mpv's decoder pipeline) instead of unload/reload;
   * the engine unit pins `--gapless=yes` (the mpv default, made explicit
     in deploy/navidrome-remote-mpv.service). Cross-format silence is a
     property of the FILES (sample-rate/layout mismatch), bounded by
   mpv's gapless engine, not fixable from the control plane: the residue
     is MEASURED (event spacing) in C1/C3, never promised away.

5. RETOMADA (A5 — crash-safe restart semantics)
   * `apply_queue` is PHASE-AWARE: it reads the transport phase the
     store last recorded (A5 schema v2 — playing / paused / stopped)
     and re-applies the queue accordingly:
       playing  -> start at the persisted index, then a best-effort
                  `seek` to the persisted position — applied ONLY when
                  that position is anchored to the exact track being
                  started (`cur_track` must match the item id) and
                  clamped to the loaded duration;
       paused   -> the start-in-pause idiom: `pause` is armed BEFORE
                  `playlist-play`, so the engine presents the very
                  "paused mid-track" picture the user left behind (a
                  bare `pause` on an idle engine would never start the
                  file); settings are mirrored BEFORE the start in the
                  phase-aware path, so the first entry inherits them;
       stopped  -> the list is loaded PARKED (idle engine holding the
                  list): a crash must never surprise the listener with
                  audio it was told to stop.
     Stores that predate the phase field (A4-era test stubs) keep the
     legacy contract exactly: start first, settings mirror after.
   * `resume()` closes the trap: a /resume against a PARKED engine
     (idle, non-empty list — the "stopped" case, or an engine that
     stopped on its own) must start the list at the persisted index;
     a genuinely paused file merely lifts `pause`; an empty list is a
     graceful no-op.

Credential invariant (unchanged from A3): stream URLs are composed HERE
(server side) from id + service account and never leave the process —
not to a client, not to disk, not to the spy (which redacts even if a
caller misbehaves).

Threading: every method is BLOCKING (it drives MpvEngine). Async callers
wrap with `asyncio.to_thread` — the Api layer does.
"""

from __future__ import annotations

from .queue import PHASE_PAUSED, PHASE_PLAYING, PHASE_STOPPED


class ReconcileResult:
    """Outcome of one engine->store reconciliation pass."""
    __slots__ = ("changed", "index")

    def __init__(self, changed: bool, index):
        self.changed = changed
        self.index = index

    def __repr__(self) -> str:
        return "ReconcileResult(changed=%r, index=%r)" % (
            self.changed, self.index)


# repeat mode -> (loop-file, loop-playlist).  mpv: 0 = off, -1 = forever,
# N >= 0 = N+1 plays.  Aliases mirror the Navidrome UI spelling (A3).
REPEAT_PLAN = {
    "off": (0, 0),
    "track": (-1, 0),
    "playlist": (0, -1),
    "all": (0, -1),
    "one": (-1, 0),
}

# Core observation set — properties guaranteed present on every mpv build
# we anchor to (and on the C1 mock). One observation PASS per snapshot:
# the stock protocol has no multi-get, so each name costs one
# get_property call (cheap on AF_UNIX datagrams).
CORE_PROPS = (
    "idle",
    "playback-status",
    "time-pos",
    "media-title",
    "shuffle",
    "loop-file",
    "loop-playlist",
    "volume",
    "duration",
    "playlist-index",
    "playlist-count",
)

# Extended probe — soft: `playlist-current-id` is the reconciliation
# anchor, but an engine lacking it must not take /status down, so it is
# fetched separately and tolerated as absent (None).
EXTENDED_PROPS = ("playlist-current-id",)


def _to_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_idle(props: dict) -> "bool | None":
    """Normalize the `idle` flag. Unknown (missing/unparsed) -> None,
    so conservative callers can abstain instead of guessing."""
    v = props.get("idle")
    if v is None:
        return None
    if isinstance(v, str):
        return v.lower() in ("yes", "true", "1", "stopped")
    return bool(v)


class PlaybackController:
    # duck-typed collaborators (Spy, MpvEngine, QueueStore) — the ADR
    # pins the PROTOCOL, not the classes; the C1 mock proves the rest.
    def __init__(self, cfg, spy, engine, store):
        self.cfg = cfg
        self.spy = spy
        self.engine = engine
        self.store = store

    # ============================================================ queue sync
    def apply_queue(self) -> bool:
        """Push the store's queue into the engine's playlist (id-tagged).

        Deterministic protocol (identical against real mpv and the C1
        mock — fake_mpv_sink v0.3):

            playlist-clear
            playlist-add  <url(i)>  <i>  <id(i)>     i = 0..n-1
            playlist-play <start>
            set_property  volume         (only when persisted)
            set_property  shuffle
            set_property  loop-file
            set_property  loop-playlist

        A3's `loadfile <u0> "replace"` + bulk-add shape is SUPERSEDED by
        clear/add/play so that every entry — including the first —
        carries an mpv `id` (the reconciliation anchor). The playback
        outcome is the same: the first entry (or the persisted index)
        plays, the rest wait in the queue. Returns True iff the engine
        acked the whole batch.

        A5 — the same protocol becomes PHASE-AWARE (module docstring
        5.): stores that carry a recorded transport phase re-apply it
        (playing -> play + anchored best-effort seek; paused ->
        pre-armed pause, then play; stopped -> park). The settings
        mirror moves BEFORE the start in that branch so the first
        entry inherits the knobs. Stores without a phase field (A4-era
        stubs) keep the original sequence verbatim.
        """
        from .api import subsonic_stream_url

        store = self.store
        if not store.items:
            store.pending_restore = False
            return True
        if not (self.cfg.nav_user and self.cfg.nav_pass):
            self.spy.event_sync("store_apply_skipped",
                                reason="sem-credenciais-navidrome",
                                items=len(store.items))
            store.pending_restore = False
            return False

        try:
            base = self.cfg.upstream_base_url
            creds = (self.cfg.nav_user, self.cfg.nav_pass)
            urls = [subsonic_stream_url(base, it.id, *creds)
                    for it in store.items]
        except Exception as exc:
            self.spy.event_sync("store_apply_failed",
                                reason="url-build", error=str(exc))
            return False

        eng = self.engine
        has_phase = hasattr(store, "phase")
        # F7 — CAPTURE THE PLAN BEFORE TOUCHING THE ENGINE. The phase
        # branch below must be decided from a snapshot taken before the
        # first IPC command: the deploy burst spends tens of
        # milliseconds (clear + N adds) during which a concurrent
        # observer tick (the watcher's first /status poll inside the
        # restore window) may legally rewrite the LIVE store (an idle
        # fact folding 'playing' -> 'stopped'). Deciding from the
        # captured plan makes the branch immutable; F7's source-lock
        # (granted below on ack) additionally keeps later ticks from
        # demolishing the just-applied picture. (The T3 retomada
        # regression read `phase` mid-burst, after the wipe had
        # landed — a 50 ms alignment coin flip between runs.)
        phase = PHASE_PLAYING  # effective phase (legacy default)
        if has_phase:
            phase = store.phase
        st = dict(store.settings)
        items_snap = list(store.items)
        try:
            eng.cmd(["playlist-clear"])
            for i, (item, url) in enumerate(zip(items_snap, urls)):
                eng.cmd(["playlist-add", url, i, item.id])
            start = store.index
            if start is None or not (0 <= start < len(items_snap)):
                start = 0
        except Exception as exc:
            self.spy.event_sync("store_apply_failed",
                                reason="ipc", error=str(exc))
            return False

        # A6.R (A5 rework) — the snapshot taken ABOVE (the captured
        # `phase` and `st = dict(store.settings)`) is the ONLY decision
        # source for the rest of this method. Re-deriving both after
        # the deploy burst was the A5 regression: the re-bound settings
        # reference re-opened the mid-burst store rewrite race F7's
        # capture exists to prevent, and the forced `phase = playing`
        # threw away the persisted picture — a transport the listener
        # left PAUSED came back PLAYING, and one left STOPPED STARTED
        # the list (the rework battery: the paused picture lost, the
        # parked list booted playing, and /resume answering "unpaused"
        # where the trapped parked list demanded "started").
        try:
            if has_phase:
                # A5: the settings mirror BEFORE the start so the first
                # entry inherits them (whichever phase branch runs).
                if st.get("volume") is not None:
                    eng.cmd(["set_property", "volume", float(st["volume"])])
                eng.cmd(["set_property", "shuffle",
                         int(bool(st.get("shuffle")))])
                loop_file, loop_pl = REPEAT_PLAN[(st.get("repeat") or "off")]
                eng.cmd(["set_property", "loop-file", loop_file])
                eng.cmd(["set_property", "loop-playlist", loop_pl])

                if phase in (PHASE_PLAYING, PHASE_PAUSED):
                    if phase == PHASE_PAUSED:
                        # the start-in-pause idiom: arm `pause` BEFORE
                        # the play, so the engine presents the exact
                        # "paused mid-track" picture the user left.
                        eng.cmd(["set_property", "pause", True])
                    eng.cmd(["playlist-play", start])
                    self._resume_position(start)
                # phase == PHASE_STOPPED: the list stays PARKED in an
                # idle engine — a crash must never surprise the
                # listener with audio it was told to stop.
            else:
                # A4-era store: the legacy contract, verbatim —
                # start first, then the settings mirror.
                eng.cmd(["playlist-play", start])
                if st.get("volume") is not None:
                    eng.cmd(["set_property", "volume", float(st["volume"])])
                eng.cmd(["set_property", "shuffle",
                         int(bool(st.get("shuffle")))])
                loop_file, loop_pl = REPEAT_PLAN[(st.get("repeat") or "off")]
                eng.cmd(["set_property", "loop-file", loop_file])
                eng.cmd(["set_property", "loop-playlist", loop_pl])
        except Exception as exc:
            self.spy.event_sync("store_apply_failed",
                                 reason="ipc", error=str(exc))
            return False

        store.pending_restore = False
        ras = getattr(store, "restore_armed", None)
        if ras:
            store.restore_armed = False
        if has_phase:
            # F7 — the picture is (re-)established: protect the fresh
            # playing/paused phase against an observation-demote while
            # the engine settles (see QueueStore.lock_phase_source).
            lsp = getattr(store, "lock_phase_source", None)
            if lsp is not None:
                lsp("apply")
        if has_phase and phase != PHASE_PLAYING:
            # A5: the restored state was deliberately NOT brought back
            # to audio (paused picture or parked list) — recorded as a
            # distinct fact so QA can audit "restart == same picture".
            self.spy.event_sync("store_apply_parked",
                                phase=phase, items=len(urls))
        else:
            self.spy.event_sync("store_apply_ok", items=len(urls))
        return True

    def _resume_position(self, start: int) -> bool:
        """A5 — best-effort position restore on a (re)started track.

        Fires ONLY when the persisted position is anchored to the exact
        track being started (`cur_track` equals the item id at
        `start`): a position captured while a DIFFERENT track was
        active (the engine advanced between the last observation and
        the crash) would land in the wrong file. The target is clamped
        to the loaded duration (defensive probe; when the engine
        reports no duration the seek goes verbatim and lands at the
        engine's own EOF boundary — the mock behaves alike). Failure
        here is a WARNING, never an apply failure: worst case the
        track simply starts at its head.
        """
        store = self.store
        pos = store.pos
        if not isinstance(pos, (int, float)) or pos <= 0:
            return False
        item = store.items[start] if 0 <= start < len(store.items) else None
        if item is None or store.cur_track != item.id:
            # anchor mismatch: the persisted position belongs to a
            # DIFFERENT track — dropping it would need a mutation; the
            # next observation pass re-anchors (and resets) the record.
            return False
        target = float(pos)
        try:
            dur = _to_float(self.engine.get_property("duration"))
        except Exception:
            dur = None
        if dur is not None and dur > 0:
            target = min(target, max(0.0, dur - 0.25))
        if target <= 0:
            return False
        try:
            self.engine.cmd(["seek", round(target, 3), "absolute"])
            # the engine is now WHERE the persisted record pointed: keep
            # the record honest (the next observation pass will refresh
            # it with the engine's own clock).
            store.set_position(target, force=False)
        except Exception as exc:
            self.spy.event_sync("store_apply_warn",
                                reason="pos-seek", error=str(exc))
        return True

    def resume(self) -> str:
        """A5 — lift a pause, or start a PARKED list.

        Three engine pictures, one user action:
          * file loaded, engine BUSY (whether presented paused via the
            start-in-pause idiom or freely playing) -> `pause false`
            (the ordinary resume);
          * engine IDLE with a NON-EMPTY list (the "stopped" parked
            restore, or an engine that stopped on its own after a
            natural EOF) -> start the list at the persisted index.
            This is THE TRAP a naive implementation misses: an idle
            engine obeys neither `pause false` nor `playlist-play`
            unless asked, so a plain "set pause to false" would leave
            a parked queue SILENT forever;
          * engine idle with an EMPTY list -> graceful no-op (there is
            nothing to resume; a ghost must not raise into the
            request path).
        The store's phase is aligned with whatever REALLY happens:
        "unpaused"/"started" -> playing (forced — the user's intent is
        a discrete transaction), "noop" leaves the record untouched.
        The phase is a FACT, and a fact is only ever written when the
        world actually changes.
        """
        props = self.engine.get_properties(["idle", "playlist-count"])
        idle = _is_idle(props)
        if idle is False:
            # busy: a single property write, nothing more.
            self.engine.cmd(["set_property", "pause", False])
            self.store.set_phase(PHASE_PLAYING, force=True)
            return "unpaused"
        if idle is not True:
            return "noop"  # unknown engine state: never guess
        try:
            cnt = int(props.get("playlist-count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt <= 0:
            return "noop"  # parked but EMPTY: nothing to start
        idx = self.store.index
        items = self.store.items
        if idx is None or not (0 <= idx < len(items)):
            idx = 0
        self.engine.cmd(["playlist-play", idx])
        self.store.mark_selected(idx, persist=True)
        self.store.set_phase(PHASE_PLAYING, force=True)
        return "started"

    # ============================================================= transport
    def play(self, url: str) -> None:
        """Direct playback (id-derived or explicit url): fresh single-file
        playlist. The caller (Api) updates the store intent (A3 rules)."""
        self.engine.cmd(["loadfile", str(url), "replace"])

    def pause(self, flag: bool) -> None:
        """The pause button: ONE stock command (`set_property pause
        <flag>`) — the A4 transport contract: one command per action,
        never an engine probe inside a button (the C1 suite asserts
        the exact command list). WHETHER that tap merits persisting a
        phase is a POLICY decision of the Api layer (A5 "records are
        facts"): the handler decides from the engine's state itself."""
        self.engine.cmd(["set_property", "pause", bool(flag)])

    def stop(self) -> None:
        self.engine.cmd(["stop"])

    def next(self) -> None:
        self.engine.cmd(["playlist-next"])

    def prev(self) -> None:
        self.engine.cmd(["playlist-prev"])

    def select(self, index: int) -> None:
        """Jump the engine to playlist position `index` (driver capability;
        the M1 route set carries no selection endpoint — reserved for the
        bridge evolution)."""
        self.engine.cmd(["playlist-play", int(index)])

    def seek(self, target: float) -> float:
        """Absolute seek, clamped to the loaded duration when the engine
        reports one (seeking past EOF just lands at the end — no spurious
        advance). Returns the EFFECTIVE target actually sent."""
        try:
            dur = _to_float(self.engine.get_property("duration"))
        except Exception:
            dur = None  # engine unresponsive on the probe: send verbatim
        eff = target
        if dur is not None and dur > 0:
            eff = min(target, dur)
        eff = max(0.0, eff)
        self.engine.cmd(["seek", round(eff, 3), "absolute"])
        return eff

    # ================================================================== modes
    def shuffle(self, value: int) -> None:
        """One stock command: the mpv `shuffle` property. On a 0->1 write
        the engine reorders its own playlist (current entry kept in place;
        the C1 mock emulates the same write-triggered reorder); 1->0 only
        clears the flag.
        """
        self.engine.cmd(["set_property", "shuffle", int(bool(value))])

    def repeat(self, mode: str) -> None:
        plan = REPEAT_PLAN.get(mode)
        if plan is None:
            raise ValueError("repeat mode inválido: %r" % (mode,))
        loop_file, loop_pl = plan
        self.engine.cmd(["set_property", "loop-file", loop_file])
        self.engine.cmd(["set_property", "loop-playlist", loop_pl])

    def volume(self, level: float) -> None:
        self.engine.cmd(["set_property", "volume", float(level)])

    # ================================================================== state
    def snapshot(self) -> dict:
        """One observation pass of the engine, then reconcile.

        Returns the props dict: CORE_PROPS (batched) plus the soft
        `playlist-current-id` probe (None when the engine lacks it).
        """
        props = self.engine.get_properties(list(CORE_PROPS))
        try:
            props["playlist-current-id"] = self.engine.get_property(
                "playlist-current-id")
        except Exception:
            props["playlist-current-id"] = None
        self.reconcile(props)
        return props

    def reconcile(self, props: dict) -> ReconcileResult:
        """Make store.index follow the ENGINE while a track is active.

        Anchor: `playlist-current-id` (our per-entry mpv ids) — immune to
        shuffle reorderings. Abstains (no write) when nothing is active
        (`idle`) or the engine reports no id (opaque/url playback): then
        the store keeps holding the intent (A3). A change is persisted
        (store.mark_selected(persist=True)) so a service restart never
        regresses an agreed state.
        """
        idx = self.store.index
        if _is_idle(props) is not False:
            return ReconcileResult(False, idx)
        cid = props.get("playlist-current-id")
        if isinstance(cid, str) and cid:
            found = self.store.find_by_id(cid)
            if found is not None and found != idx:
                self.store.mark_selected(found, persist=True)
                return ReconcileResult(True, found)
        return ReconcileResult(False, idx)

    def active_with_track(self) -> bool:
        """Alive, explicitly not idle, and serving at least one playlist
        entry (the 503 engine-idle gate for next/prev — A3 semantics on a
        richer basis: `playlist-count` replaces the legacy size probe)."""
        try:
            if not self.engine.alive:
                return False
        except Exception:
            return False
        try:
            props = self.engine.get_properties(["idle", "playlist-count"])
        except Exception:
            return False
        if _is_idle(props) is not False:
            return False
        try:
            return int(props.get("playlist-count") or 0) > 0
        except (TypeError, ValueError):
            return False
