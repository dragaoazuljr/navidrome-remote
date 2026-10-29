"""Persistent queue/state store (spec v1.2, M1 — card A3; hardened — card A5).

The `queue` route of /api/v1/* implies a playlist the service REMEMBERS
across mpv restarts and service reboots (definition-of-ready item 5:
"a reprodução é independente da sessão do navegador" + reboot recovery).
This module is that memory.

A5 — crash-safe persistence, serialized writes, retomada
=========================================================
Serialization (zero-lost-update races)
--------------------------------------
* Every mutation runs under a single lock. One dedicated writer thread
  is the ONLY component that ever touches the state file; the event
  loop and worker threads (reconciliation, /status) mutate memory only.
* Discrete user transactions (queue replace/append, play, pause,
  resume, stop, next/prev step, an engine->store index AGREEMENT via
  `mark_selected(persist=True)`) are SYNC-flushed: dying mid-transaction
  never loses the transaction's effect.
* Continuous observations (position sampling, phase seen by the 5 s
  state tracker, settings tweaks) are COALESED by the writer: at most
  one physical write per window (default 1.0 s — NVRA_STATE_WRITE_
  INTERVAL). A sudden death inside a window loses at most one window
  of continuous progress; the engine (a separate unit, Restart=always)
  is the source of truth while alive and re-drives the store on the
  first /status.

Atomicity (crash-safe)
----------------------
Render = tmp file in the SAME directory + full write + fsync(file) +
chmod 0600 + rename over the target + best-effort fsync(directory).
The file on disk is, at any instant, EITHER the previous coherent
state OR the new one — never torn. The state directory is 0700
(other local users cannot enumerate the file).

Versioning
----------
The file carries `version`. An OLDER known version (v1, strict subset)
loads normally — under the v1 writer's legacy contract the persisted
queue resumes PLAYING (A3 promise: "reboot recovery") — and is
re-emitted at the current version. A file at a HIGHER version than
this binary understands is LOCKED: the store starts empty, NEVER
overwrites the foreign file, raises a spy signal (`store_version_
locked`) and the banner warns — an operator inspects or moves the
file. Unreadable (garbage) content is QUARANTINED: renamed to
`<path>.corrupt-<stamp>` (evidence kept), the store starts empty and
the next render simply rebuilds the file; a spy signal
(`store_state_quarantined`) lands in the NDJSON trail.

Retomada (what a restart does)
------------------------------
Schema v2 records, besides the queue intent, the TRANSPORT PHASE last
seen (playing / paused / stopped — the engine's playback-status) and
the last POSITION of the active track, anchored by `cur_track` (the
song id being served) so a stale position from a DIFFERENT track can
never be applied. On re-application to the engine (apply_queue,
delegated from the lazy restore loop):

  phase = playing  -> reload the list, start at the persisted index,
                      then seek to the persisted position (clamped to
                      the loaded duration; no duration / anchor
                      mismatch -> the track simply starts at its head).
  phase = paused   -> reload the list, pre-arm `pause` and start: the
                      engine presents the exact "paused mid-track"
                      picture the user left behind (seamless /status),
                      positioned at the persisted point.
  phase = stopped  -> reload the list PARKED (idle engine holding the
                      list): a crash must never surprise the listener
                      with audio it told the service to stop.
                      `/resume` starts a parked list from the
                      persisted index; `/play` does the same for a
                      chosen id.

The principle: restart reconstructs WHAT the user built and resumes
WHERE it was only if it was moving; otherwise it parks. Nothing in
the file can make audio start that the user did not ask for.

Invariant compliance by construction
------------------------------------
The on-disk file and every in-memory view that may travel to a client
contain `id` and `title` ONLY. Stream URLs are DERIVED at apply-time
from `id` + the service account (they live in RAM for the lifetime of
one apply, then are dropped with the request context). No URL, token,
salt or credential is EVER serialized to disk or to a response.

Apply semantics (engine is the source of truth for PLAYBACK; this
store is the source of truth for QUEUE INTENT)
----------------------------------------------
* `apply_to_engine()` delegates to `controller.PlaybackController.
  apply_queue` (A4 protocol, A5 phase-aware): playlist-clear, one
  `playlist-add <url> <i> <id>` per entry, then the phase branch
  above, then the mirrored settings (volume / shuffle / repeat loops).
* It is called by a lazy background task (best effort): it retries
  while the engine is down and stops once acknowledged (the engine's
  ack is the receipt). A failure NEVER propagates into a client
  request — the worst case is a late or missed restore, logged in the
  spy (`store_apply_*`).
* `find_by_id()` / `mark_selected(persist=True)` back the A4
  engine->store reconciliation: while a track is active the store's
  index follows `playlist-current-id` (immune to shuffle) and the
  agreement is DURABLE (persist=True sync-flushes; a service restart
  never regresses an agreed state).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time

from .spy import Spy

STORE_VERSION = 2
#: Versions this binary KNOWS how to read (strict-subset rule: an older
#: known version loads and is re-emitted at STORE_VERSION).
SUPPORTED_VERSIONS = (1, 2)
MAX_ITEMS = 64
MAX_TITLE_LEN = 160

#: Transport phases — the engine's `playback-status` vocabulary as the
#: store records it. The real engine spells the idle state "stop"; the
#: C1 mock spells it "stopped"; both normalize to the same phase here.
PHASE_PLAYING = "playing"
PHASE_PAUSED = "paused"
PHASE_STOPPED = "stopped"
_KNOWN_PHASES = (PHASE_PLAYING, PHASE_PAUSED, PHASE_STOPPED)

#: Minimum position delta (s) worth a continuous sample. Sampling
#: below this step is dropped before it even marks the snapshot dirty
#: (wear budget: position ticks arrive with every /status poll).
POS_SAMPLE_STEP = 5.0

DEFAULT_WRITE_WINDOW = 1.0  # s; overridable via NVRA_STATE_WRITE_INTERVAL

STATE_DEFAULT_PATH = "/var/lib/navidrome-remote/state.json"


class StoreError(ValueError):
    """Operator-facing validation failure (mapped to 400 by the API)."""


class QueueItem:
    __slots__ = ("id", "title")

    def __init__(self, id: str, title: "str | None" = None):
        self.id = str(id)
        self.title = (str(title)[:MAX_TITLE_LEN] if title else None)

    def to_safe_dict(self) -> dict:
        out: dict = {"id": self.id}
        if self.title:
            out["title"] = self.title
        return out  # no url. no token. nothing a client should not hold.


def _normalize_phase(raw) -> "str | None":
    """Map engine spellings to the store's phase vocabulary (or None
    when unrecognized — conservative callers keep the current phase)."""
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if v in ("playing", "play"):
        return PHASE_PLAYING
    if v == "paused":
        return PHASE_PAUSED
    if v in ("stop", "stopped", "idle"):
        return PHASE_STOPPED
    return None


def _to_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class QueueStore:
    def __init__(self, cfg, spy: Spy):
        self.cfg = cfg
        self.spy = spy
        self.path = getattr(cfg, "state_file", STATE_DEFAULT_PATH)
        try:
            self._win = max(0.05, min(60.0,
                                      float(getattr(
                                          cfg, "state_write_interval",
                                          DEFAULT_WRITE_WINDOW))))
        except (TypeError, ValueError):
            self._win = DEFAULT_WRITE_WINDOW

        self.items: "list[QueueItem]" = []
        self.index: "int | None" = 0
        self.settings: dict = {"volume": None, "shuffle": 0, "repeat": "off"}
        #: A5 — transport phase last observed/set ("playing"|"paused"|
        #: "stopped"). v1 files load as "playing" (their writer's
        #: legacy contract: the persisted queue resumes playing).
        self.phase: str = PHASE_STOPPED
        #: A5 — last position of the ACTIVE track (s); None when idle.
        self.pos: "float | None" = None
        #: A5 — song id the `pos` belongs to (anchor: a position from a
        #: different track must never be applied at resume).
        self.cur_track: "str | None" = None
        #: A5 — logical clock of mutations (diagnostic; monotone by
        #: construction of the single-lock model).
        self.seq: int = 0
        #: A5 — physical renders performed since boot (diagnostics; the
        #: write counter a wear policy can observe on the SBC).
        self.writes: int = 0
        #: A5 — the on-disk file is at a version this binary does not
        #: know: it is protected (never overwritten) until an operator
        #: intervenes; the writer stays disarmed in the meantime.
        self.version_locked: bool = False
        #: A5 — path of a quarantined unreadable predecessor ("''" when
        #: none), kept for the banner/spy trail.
        self.quarantined: "str | None" = None

        self._lock = threading.RLock()
        self._dirty = False
        self._closing = False
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._wthread: "threading.Thread | None" = None

        self.pending_restore = False
        # F7b — the RESTORE-window latch: armed ONLY by the loader
        # (an image read from disk that must (re)appear on the engine)
        # and cleared when the apply acknowledges. Plain user
        # transactions (replace/append/note_replacement) set
        # `pending_restore` for the lazy apply's benefit but do NOT
        # arm the restore window: in those cases the service and
        # engine are alive and observations keep the A5 continuous-
        # state contract (the A6.2 overload blocked them and broke
        # the A5 close-drain/round-trip persistence).
        self.restore_armed = False
        # F7 — race fences (in-memory only, see lock_phase_source).
        self._phase_source = "user"
        self._apply_since = 0.0
        self._loaded = self._load()
        # A5 wear budget: the EARLIEST timestamp of the NEXT physical
        # write. The writer thread sleeps until the slot, and a wake
        # shortens the SLEEP (a close request) but NEVER the slot —
        # this separation is what bounds continuous observations to at
        # most one write per window instead of one write per sample.
        self._slot_until = time.monotonic() + self._win
        if not self.version_locked:
            self._start_writer()

    # ------------------------------------------------------------------ io
    def _load(self) -> bool:
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw_text = fh.read()
            obj = json.loads(raw_text)
            if not isinstance(obj, dict):
                raise ValueError("root not an object")
        except (OSError, ValueError) as exc:
            self._quarantine(type(exc).__name__)
            return False

        version = obj.get("version")
        if not isinstance(version, int) or version not in SUPPORTED_VERSIONS:
            # UNKNOWN (or future) version: the file is NOT ours to edit.
            # Lock the writer, start empty, tell the operator.
            self.version_locked = True
            self._warn(
                "store_version_locked",
                version=str(version),
                hint="versao do estado desconhecida — nao sobrescrito; "
                     "inspecionar/mover o arquivo para continuar",
            )
            return False

        items = []
        for raw in obj.get("items", [])[:MAX_ITEMS]:
            if not isinstance(raw, dict):
                continue
            iid = raw.get("id")
            if isinstance(iid, str) and iid:
                items.append(QueueItem(iid, raw.get("title")))
        self.items = items
        idx = obj.get("index")
        self.index = (int(idx)
                      if isinstance(idx, int) and 0 <= idx < max(1, len(items))
                      else (0 if items else None))
        st = obj.get("settings", {})
        if isinstance(st, dict):
            if isinstance(st.get("volume"), (int, float)):
                self.settings["volume"] = st["volume"]
            if isinstance(st.get("shuffle"), (int, bool)):
                self.settings["shuffle"] = int(bool(st["shuffle"]))
            rep = st.get("repeat")
            if rep in ("off", "track", "playlist"):
                self.settings["repeat"] = rep
        else:
            st = {}

        sq = obj.get("seq")
        if isinstance(sq, int) and sq >= 0:
            self.seq = sq  # the logical clock CONTINUES across restarts

        if version >= 2:
            self.phase = _normalize_phase(obj.get("phase")) or PHASE_STOPPED
            self.pos = _to_float(obj.get("pos"))
            ct = obj.get("cur_track")
            self.cur_track = ct if isinstance(ct, str) and ct else None
            if self.pos is not None and self.cur_track is None:
                self.pos = None  # unanchored position: unusable at resume
            if self.pos is not None and self.phase != PHASE_PLAYING \
                    and self.phase != PHASE_PAUSED:
                self.pos = None  # a stopped session carries no position
        else:
            # v1: the A3 writer's contract — a persisted queue resumes
            # PLAYING on boot. v2 is where the finer phase knowledge starts.
            self.phase = PHASE_PLAYING

        self.pending_restore = bool(items)
        self.restore_armed = bool(items)
        return True

    def _quarantine(self, reason: str) -> None:
        """Preserve an unreadable predecessor as evidence, then go on
        empty (the next render rebuilds a healthy file)."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                        exist_ok=True)
            self._secure_dir()
            self.quarantined = "%s.corrupt-%s" % (self.path, stamp)
            os.replace(self.path, self.quarantined)
        except OSError:
            # could not move it (permissions, read-only fs): the next
            # atomic render will simply overwrite it with a valid state.
            self.quarantined = None
        self._warn(
            "store_state_quarantined",
            reason=reason,
            size=os.path.getsize(self.path) if os.path.exists(self.path)
            else -1,
            moved=self.quarantined or "",
        )

    def _warn(self, kind: str, **fields) -> None:
        """Constructor-time diagnostics (no event loop yet): the
        synchronous spy channel carries them."""
        try:
            self.spy.event_sync(kind, **fields)
        except Exception:
            pass
        print("[store] %s: %s" % (kind, fields), flush=True)

    # ------------------------------------------------------------- renderer
    def _secure_dir(self) -> None:
        """State directory 0700: the file is 0600; its NAME should not
        be enumerable by other local users either."""
        try:
            dirname = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(dirname, exist_ok=True)
            os.chmod(dirname, 0o700)
        except OSError:
            pass  # durability is best-effort; the control plane must
                  # never die because of the store's home

    def _physical_write(self, data: bytes) -> None:
        """The atomic render: tmp + fsync + 0600 + rename (+dir fsync).

        A single code path, shared by the writer thread and the
        sync-flush requests — the file can only ever hold a COMPLETE
        snapshot (crash-safe by construction of the rename). Kept as a
        seam so tests may inject a mid-write fault.
        """
        dirname = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(dirname, exist_ok=True)
        self._secure_dir()
        fd, tmp = tempfile.mkstemp(prefix=".nvr-state-", dir=dirname)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.rename(tmp, self.path)
        try:
            dfd = os.open(dirname, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass  # the rename itself is atomic; dir persistence is
                  # best-effort (a power cut at worst drops the file to
                  # its previous state — never tears it)

    def _render(self, force: bool = False) -> bool:
        """Serialize the CURRENT in-memory state to disk.

        Called under the store lock: the snapshot taken and the bytes
        written are the same, and renders happen in temporal order, so
        the file can only move FORWARD in time (a stale snapshot can
        never clobber a fresher one). Sync-flush requests run inline
        (holding the lock — a sub-KB write), windowed renders run in
        the writer thread; both serialize on this same lock.
        Returns True if bytes were written.
        """
        if self.version_locked:
            return False  # the foreign file is protected (see __init__)
        if not force and not self._dirty:
            return False
        self._dirty = False
        obj = {
            "version": STORE_VERSION,
            "seq": self.seq,
            "items": [it.to_safe_dict() for it in self.items],
            "index": self.index,
            "phase": self.phase,
            "pos": (round(self.pos, 3)
                    if self.pos is not None else None),
            "cur_track": self.cur_track,
            "settings": {
                "volume": self.settings.get("volume"),
                "shuffle": int(bool(self.settings.get("shuffle"))),
                "repeat": self.settings.get("repeat", "off"),
            },
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            data = json.dumps(obj, separators=(",", ":"),
                              ensure_ascii=False).encode()
            self._physical_write(data)
            self.writes += 1
            return True
        except OSError as exc:
            # durability is best-effort: a broken store path must never
            # take down the control plane (same philosophy as the spy).
            print("[store] falha gravando %s: %s" % (self.path, exc),
                  flush=True)
            self._dirty = True  # try again on the next window
            return False

    def _start_writer(self) -> None:
        if self._wthread is not None:
            return
        self._wthread = threading.Thread(
            target=self._writer_loop, name="nvr-store-writer", daemon=True)
        self._wthread.start()

    def _writer_loop(self) -> None:
        """The single writer: at most ONE physical write per window
        (the 2W wear budget), plus the drain-flush on close.

        The subtlety that used to defeat the window: `Event.wait
        (timeout)` returns INSTANTLY when the event is already set, so
        a burst of windowed kicks turned into one write PER SAMPLE —
        the exact SD-card burn A5 forbids. The fix separates the two
        clocks: `_wake` shortens the SLEEP (it is the urgent line for
        `close()`), while `_slot_until` — never touched by wakes —
        short-circuits the WRITE. A dirty snapshot therefore lands at
        most once per window; continuous observations pile up in
        memory and the slot flushes the newest one."""
        while True:
            wait_s = max(0.0, self._slot_until - time.monotonic())
            if wait_s > 0:
                self._wake.wait(timeout=wait_s)
                self._wake.clear()
            if self._closing:
                # shutdown interrupts the slot: the last state must
                # land now (drain) — urgency beats the wear budget.
                with self._lock:
                    self._render(force=True)
                self._closed.set()
                break
            if time.monotonic() < self._slot_until - 1e-3:
                continue  # woke early (a kick): sleep the remainder
            with self._lock:
                if self._dirty:
                    # the slot is CONSUMED regardless of success: a
                    # failing render retried on the next window (never
                    # a spin against a broken path).
                    self._render()
                    self._slot_until = time.monotonic() + self._win

    def _kick(self, force: bool) -> None:
        """Request attention from the writer (windowed) or sync-flush
        (force). Called with the store lock HELD by the mutators."""
        if self._closing:
            return
        if force:
            self._render(force=True)
        else:
            self._wake.set()

    def close(self) -> None:
        """Clean shutdown: disarm, wake the writer, drain, final flush.
        Idempotent (safe to call from a teardown race)."""
        with self._lock:
            self._closing = True
            self._wake.set()
        if self._wthread is not None:
            self._wthread.join(timeout=max(2.0, 3 * self._win))
        with self._lock:
            self._render(force=True)
            self._closed.set()

    # ------------------------------------------------------- mutation (A5)
    def _mutate(self, fn, force: bool) -> None:
        """Uniform entry point: lock -> apply -> advance the logical
        clock -> serialize (sync-flush for discrete events, windowed
        for continuous ones)."""
        with self._lock:
            fn()
            self.seq += 1
            self._dirty = True
            self._kick(force)

    def replace(self, items: "list") -> None:
        """Discrete user transaction (force)."""
        def fn():
            new = [_coerce(it) for it in items]
            if not new:
                raise StoreError("fila vazia (mínimo 1 item)")
            self.items = new
            self.index = 0
            # a fresh queue replaces whatever track was active
            self.pos = None
            self.cur_track = None
            self.pending_restore = True
        self._mutate(fn, force=True)

    def append(self, items: "list") -> None:
        """Discrete user transaction (force)."""
        def fn():
            if len(self.items) + len(items) > MAX_ITEMS:
                raise StoreError("fila excede %d itens" % MAX_ITEMS)
            self.items.extend(_coerce(it) for it in items)
            if self.index is None and self.items:
                self.index = 0
            self.pending_restore = True
        self._mutate(fn, force=True)

    def mark_selected(self, idx: "int | None", persist: bool = False) -> None:
        """Point the intent selector at a queue position.

        `persist=True` (A4 reconciliation path): the agreement with the
        engine is DURABLE — it sync-flushes (the A4 contract, now
        literal rather than asymptotic): a service restart never
        regresses a state the engine was actually serving.
        `persist=False`: windowed (internal bookkeeping).
        """
        wanted = idx
        def fn():
            sel = wanted
            if sel is not None and not (0 <= sel < len(self.items)):
                sel = max(0, len(self.items) - 1) if self.items else None
            self.index = sel
        self._mutate(fn, force=persist)

    def find_by_id(self, item_id: "str") -> "int | None":
        """Index of the queue item with this id (A4 reconciliation
        anchor), or None when the id is not in the queue."""
        want = str(item_id)
        for i, it in enumerate(self.items):
            if it.id == want:
                return i
        return None

    def step(self, delta: int) -> "int | None":
        """Discrete user transaction (next/prev tap — force)."""
        def fn():
            if not self.items or self.index is None:
                return
            n = len(self.items)
            rep = self.settings.get("repeat")
            if delta > 0:
                nxt = self.index + 1
                if nxt >= n:
                    nxt = 0 if rep == "playlist" else n - 1
            else:
                nxt = self.index - 1
                if nxt < 0:
                    nxt = 0
            self.index = nxt
        self._mutate(fn, force=True)

    def touch_settings(self, volume=None, shuffle=None, repeat=None) -> None:
        """Explicit UI knob (DISCRETE transaction — sync flush, ADR-004).

        A volume/shuffle/repeat tap is a deliberate user act, NOT a
        continuous observation: a sudden death right after the tap
        must not roll the knob back (the M1 persistence contract —
        "reboot restores the settings the listener last set" — rests
        on this). Tap frequency is user-paced (a few per day at the
        kiosk), so the flush cost is immaterial next to the wear
        budget, which is dominated by the windowed POSITION sampler."""
        def fn():
            if volume is not None:
                self.settings["volume"] = volume
            if shuffle is not None:
                self.settings["shuffle"] = int(bool(shuffle))
            if repeat is not None:
                self.settings["repeat"] = repeat
        self._mutate(fn, force=True)

    # F7 — how long a (re)apply keeps the phase locked against
    # observation-demote. Long enough to cover engine deploy + settle
    # latency under load; a natural stop landing after this window is
    # folded normally.
    APPLY_LOCK_S = 20.0

    def lock_phase_source(self, source: str) -> None:
        """F7 — record WHO last decided the transport phase.

        'apply': the restore apply (re-)established the picture (the
        engine is transiently idle around a (re)deploy); 'user': an
        explicit user transaction; 'obs': a folded observation. While
        'apply' is fresh (< APPLY_LOCK_S), `track_observed` may not
        demote a playing/paused phase to 'stopped' — the idle fact in
        that window is a transient of the deploy, not an intent
        (the T3 retomada regression: a watcher's first /status tick
        landed milliseconds after the apply acknowledged and rewrote
        'playing' into 'stopped', parking the engine). The lock is a
        race fence in memory only — the persisted phase remains the
        record. A user gesture (`set_phase(force=True)`) unlocks.
        """
        if source in ("apply", "user", "obs"):
            self._phase_source = source
            self._apply_since = time.monotonic() if source == "apply" \
                else 0.0

    def set_phase(self, phase: str, force: bool = True) -> None:
        """Record the transport phase (A5).

        User transactions (play/pause/resume/stop) pass force=True —
        the phase IS the transaction. Observations (the 5 s state
        tracker) pass force=False (windowed): a natural stop or EOF is
        a transition worth catching, never a burst.
        """
        norm = _normalize_phase(phase)
        if norm is None:
            return
        target = norm

        def fn():
            if self.phase != target:
                self.phase = target
            if target == PHASE_STOPPED:
                self.pos = None
                self.cur_track = None
            if force:
                # F7 — a user transaction IS the newest fact: unlock
                # any apply-imposed protection.
                self._phase_source = "user"
                self._apply_since = 0.0
        self._mutate(fn, force=force)

    def set_position(self, pos: "float | None", force: bool = False) -> None:
        """Sample the position of the active track (continuous).

        Throttled by POS_SAMPLE_STEP: a sub-step sample is dropped
        BEFORE marking the snapshot dirty (wear budget — positions tick
        with every /status poll). Track changes (handled by
        track_observed) reset it immediately, bypassing the step.
        """
        pos = _to_float(pos)
        if pos is None:
            return

        def fn():
            cur = self.pos
            if cur is not None and abs(pos - cur) < POS_SAMPLE_STEP:
                return  # below the sampling step: not worth a page
            self.pos = pos
        self._mutate(fn, force=force)

    def track_observed(self, props: dict) -> None:
        """Feed the store from one engine observation pass (the 5 s
        state tracker and /status both converge here).

        Keeps phase/position fresh without any client polling:
        * an ACTIVE engine updates the phase from `playback-status`
          and samples `time-pos` (throttled);
        * a NEW current track (the `playlist-current-id` anchor) resets
          the position to zero — this is what makes a position from a
          previous track impossible at resume;
        * an IDLE engine records "stopped" and clears the position.
        Windowed by construction: the writer coalesces bursts.
        """
        if self.version_locked:
            return
        if self.restore_armed:
            # A6.2 (F7b) — the RESTORE window: while a LOADED picture
            # is not yet applied, the engine is TRANSIENTLY idle (or
            # mid-apply) — that is not the user's transport intent.
            # Folding such an observation would rewrite the awaited
            # phase (e.g. paused-mid-track -> "stopped") and erase the
            # position anchor BEFORE the lazy apply lands, turning a
            # present/positioned restore into a parked no-op. The
            # regression was invisible to per-command sampling (a torn
            # read often delivered `idle=None` and skipped the wipe)
            # and was exposed by the atomic batch, which makes the
            # `idle` fact always arrive. Observations resume the moment
            # the apply acknowledges (apply_queue clears the flag).
            return
        idle = props.get("idle")
        active = False
        if idle is not None:
            active = not (idle if isinstance(idle, bool)
                          else str(idle).lower() in ("yes", "true", "1"))
        if not active:
            # F7 — apply-imposed fence: the engine is TRANSIENTLY idle
            # around a (re)deploy for < APPLY_LOCK_S; folding
            # 'stopped' in that window is the T3 retomada regression
            # (a watcher tick milliseconds after the apply's ack
            # rewrote an awaited 'playing'/'paused' into 'stopped' and
            # parked the engine). A natural stop that genuinely
            # outlives the window, or any user gesture, demotes as
            # before (set_phase(force=True) resets the source).
            fresh_apply = (getattr(self, "_phase_source", "user")
                           == "apply" and self._apply_since > 0.0
                           and (time.monotonic() - self._apply_since)
                           < self.APPLY_LOCK_S)
            if fresh_apply and self.phase in (PHASE_PLAYING,
                                              PHASE_PAUSED):
                return
            def fn():
                if self.phase != PHASE_STOPPED:
                    self.phase = PHASE_STOPPED
                self._phase_source = "obs"
                if self.pos is not None or self.cur_track is not None:
                    self.pos = None
                    self.cur_track = None
            self._mutate(fn, force=False)
            return

        cid = props.get("playlist-current-id")
        phase = _normalize_phase(props.get("playback-status")) or PHASE_PLAYING
        pos = _to_float(props.get("time-pos"))

        def fn():
            if isinstance(cid, str) and cid and cid != self.cur_track:
                self.cur_track = cid
                self.pos = 0.0
            elif cid is None:
                # the engine refuses to say WHICH id (opaque play): do
                # not trust a position we cannot anchor
                self.cur_track = None
                self.pos = None
            elif pos is not None:
                cur = self.pos
                if cur is None or abs(pos - cur) >= POS_SAMPLE_STEP:
                    self.pos = pos
            if phase != self.phase:
                self.phase = phase
                if phase == PHASE_STOPPED:
                    self.pos = None
        self._mutate(fn, force=False)

    def note_replacement(self, played: "dict") -> None:
        """A direct play (url/id outside the queue) resets the queue to
        that single item, keeping views consistent. Discrete (force).
        """
        iid = played.get("id")
        if isinstance(iid, str) and iid:
            def fn():
                self.items = [QueueItem(iid, played.get("title"))]
                self.index = 0
                self.pending_restore = True
                self.phase = PHASE_PLAYING
                self.pos = 0.0
                self.cur_track = iid
            self._mutate(fn, force=True)
            return
        def fn():
            # opaque url play: the engine leaves the queue, but the
            # QUEUE INTENT survives (suspended: index=None) — wiping it
            # would destroy a playlist the user built.
            if self.items:
                self.index = None
            self.phase = PHASE_STOPPED
            self.pos = None
            self.cur_track = None
        self._mutate(fn, force=True)

    # ---------------------------------------------------------------- views
    def safe_snapshot(self) -> dict:
        """Sanitized: what a client may lawfully receive."""
        return {
            "length": len(self.items),
            "index": self.index,
            "items": [it.to_safe_dict() for it in self.items],
        }

    def settings_snapshot(self) -> dict:
        return {
            "volume": self.settings.get("volume"),
            "shuffle": int(bool(self.settings.get("shuffle"))),
            "repeat": self.settings.get("repeat", "off"),
            "phase": self.phase,  # A5 additive: the recorded transport phase
        }

    @property
    def loaded(self) -> bool:
        return self._loaded


def _coerce(item: "dict | QueueItem") -> QueueItem:
    if isinstance(item, QueueItem):
        return item
    if not isinstance(item, dict):
        raise StoreError("item inválido (objeto ou QueueItem esperado)")
    iid = item.get("id")
    if not isinstance(iid, str) or not iid.strip():
        raise StoreError("item sem 'id' válido")
    title = item.get("title")
    if title is not None and not isinstance(title, str):
        title = str(title)
    qi = QueueItem(iid.strip(), title)
    if "url" in item and item["url"] not in (None, ""):
        # refuse at the boundary: a raw URL in the queue is the exact
        # vector the invariant bans (client-directed fetch in mpv).
        raise StoreError("'url' não é aceito na fila (invariante de credenciais)")
    return qi


async def apply_to_engine(store: QueueStore, engine, spy: Spy) -> bool:
    """Apply the store's queue to the engine (A4 protocol, A5
    phase-aware; see PlaybackController.apply_queue).

    Signature retained from A3 (tests and the lazy restore loop call it
    directly). Best effort by contract: returns True iff the engine
    acked the whole batch. Failures log `store_apply_failed` and leave
    `pending_restore` set (the lazy task retries). The blocking IPC
    work runs off the event loop.
    """
    import asyncio

    from .controller import PlaybackController

    ctrl = PlaybackController(store.cfg, spy, engine, store)
    return await asyncio.to_thread(ctrl.apply_queue)
