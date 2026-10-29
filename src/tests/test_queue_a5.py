"""A5 — queue store hardening (ADR-004), unit level.

The store is the SOLE persistence point of the service: queue, settings,
the TRANSPORT PHASE, the observed position (anchored by the active track
id), and the monotonic revision counter. Everything here runs against
the real `navidrome_remote.queue.QueueStore` on a tmp state file + real
Spy — no engine, no upstream:

  * schema v2 round-trip (phase/pos/cur_track/seq survive load) + file
    permissions (0600 file, 0700 home);
  * LEGACY v1 file loads as the `playing` intent (A3 contract preserved)
    and is re-emitted at v2;
  * v2 `stopped` files carry no position (nothing to seek to after a
    deliberate silence);
  * FUTURE schema version => the file is LOCKED (byte-identical after
    mutation attempts; writer disarmed; spy signal);
  * CORRUPT file => QUARANTINE (renamed with evidence, spy signal, fresh
    start);
  * CONCURRENCY: eight workers mutating in parallel => the revision
    counter advances by EXACTLY the number of applied mutations (zero
    lost updates), the end state is coherent, the file is valid JSON —
    the headline acceptance ("corridas serializadas (nula perda de
    estado)"), proved at the lowest layer;
  * COALESCING WINDOW: a sustained burst of observations collapses to a
    bounded number of physical writes (the 2W SD-card wear budget) and
    close() DRAINS the pending window (crash-proof shutdown).
"""

from __future__ import annotations

import glob
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from navidrome_remote.queue import (
    PHASE_PAUSED,
    PHASE_PLAYING,
    PHASE_STOPPED,
    STORE_VERSION,
    QueueItem,
    QueueStore,
)
from navidrome_remote.spy import Spy


# ---------------------------------------------------------------- harness
def _mk(tmp: str, name: str = "state.json") -> str:
    return os.path.join(tmp, name)


def _mkcfg(tmp: str, win: float = 1.0,
           name: str = "state.json") -> SimpleNamespace:
    # the store reads `state_file` and `state_write_interval` from the
    # config; a SimpleNamespace keeps the unit hermetic (no full Config).
    return SimpleNamespace(state_file=_mk(tmp, name),
                           state_write_interval=win)


def _spy(tmp: str) -> Spy:
    return Spy(os.path.join(tmp, "spy.ndjson"))


def _events(sp: Spy) -> "list":
    out = []
    if not os.path.exists(sp.path):
        return out
    with open(sp.path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _facts(sp: Spy, kind: str) -> "list":
    return [e for e in _events(sp) if e.get("event") == kind]


ITEMS = [
    {"id": "song-a", "title": "A"},
    {"id": "song-b", "title": "B"},
    {"id": "song-c", "title": "C"},
]


def _items() -> "list[QueueItem]":
    return [QueueItem(**d) for d in ITEMS]


class V2RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="q5-rt-")
        self.sp = _spy(self.tmp)

    def tearDown(self):
        os.system(f"rm -rf {self.tmp}")

    def test_roundtrip_carries_the_full_picture(self):
        store = QueueStore(_mkcfg(self.tmp), self.sp)
        store.replace(_items())
        store.mark_selected(1)
        store.set_phase(PHASE_PAUSED)
        # two observation passes: the FIRST anchors the track id (the
        # record resets the position to zero), the SECOND lands the
        # position the user had reached.
        store.track_observed({"idle": False,
                              "playback-status": "paused",
                              "playlist-current-id": "song-b",
                              "time-pos": 0.0})
        store.track_observed({"idle": False,
                              "playback-status": "paused",
                              "playlist-current-id": "song-b",
                              "time-pos": 37.25})
        self.assertEqual(store.phase, PHASE_PAUSED)
        self.assertAlmostEqual(float(store.pos or 0), 37.25)
        self.assertEqual(store.cur_track, "song-b")
        store.close()

        reader = QueueStore(_mkcfg(self.tmp), self.sp)
        try:
            self.assertEqual([i.id for i in reader.items],
                              ["song-a", "song-b", "song-c"])
            self.assertEqual(reader.index, 1)
            self.assertEqual(reader.phase, PHASE_PAUSED)
            self.assertAlmostEqual(float(reader.pos or 0), 37.25)
            self.assertEqual(reader.cur_track, "song-b")
            self.assertEqual(reader.settings.get("volume"), None)
            self.assertGreater(reader.seq, 0)
            # the file on disk is the current version, carrying the same
            # picture (the loader re-emits migrations).
            with open(_mk(self.tmp), encoding="utf-8") as fh:
                ondisk = json.load(fh)
            self.assertEqual(ondisk["version"], STORE_VERSION)
            self.assertEqual(ondisk["phase"], PHASE_PAUSED)
            self.assertEqual(ondisk["cur_track"], "song-b")
        finally:
            reader.close()

    def test_file_permissions_file_0600_home_0700(self):
        store = QueueStore(_mkcfg(self.tmp), self.sp)
        store.replace(_items())
        store.close()
        mode = stat.S_IMODE(os.stat(_mk(self.tmp)).st_mode)
        self.assertEqual(mode, 0o600)
        homedir = os.path.dirname(os.path.abspath(_mk(self.tmp)))
        self.assertEqual(stat.S_IMODE(os.stat(homedir).st_mode), 0o700)


class LegacyV1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="q5-v1-")
        self.sp = _spy(self.tmp)

    def tearDown(self):
        os.system(f"rm -rf {self.tmp}")

    def test_v1_file_loads_as_playing_intent(self):
        payload = {
            "version": 1,
            "items": [{"id": d["id"], "title": d["title"]} for d in ITEMS],
            "index": 2,
            "settings": {"volume": 42, "shuffle": True, "repeat": "off"},
        }
        with open(_mk(self.tmp), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

        store = QueueStore(_mkcfg(self.tmp), self.sp)
        try:
            self.assertEqual(len(store.items), 3)
            self.assertEqual(store.index, 2)
            # A3 contract UNCHANGED: the A3-era writer persisted the
            # playing intent — a legacy file resumes playing.
            self.assertEqual(store.phase, PHASE_PLAYING)
            self.assertIsNone(store.pos)
            self.assertIsNone(store.cur_track)
            self.assertEqual(store.settings["volume"], 42)
            self.assertEqual(store.settings["shuffle"], 1)
            self.assertTrue(store.pending_restore)
        finally:
            store.close()


class V2StoppedPosRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="q5-st-")
        self.sp = _spy(self.tmp)

    def tearDown(self):
        os.system(f"rm -rf {self.tmp}")

    def test_stopped_file_ships_without_position(self):
        payload = {
            "version": 2,
            "seq": 7,
            "items": [{"id": "s1", "title": "T1"}],
            "index": 0,
            "phase": "stopped",
            "pos": 44.5,          # a leftover that must NOT survive
            "cur_track": "s1",
            "settings": {"volume": None, "shuffle": 0, "repeat": "off"},
        }
        with open(_mk(self.tmp), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        store = QueueStore(_mkcfg(self.tmp), self.sp)
        try:
            self.assertEqual(store.phase, PHASE_STOPPED)
            self.assertIsNone(store.pos)
        finally:
            store.close()

    def test_unanchored_position_dropped(self):
        payload = {
            "version": 2, "seq": 3,
            "items": [{"id": "s1"}], "index": 0,
            "phase": "playing", "pos": 12.0,
            "cur_track": None,      # no anchor: unusable at resume
            "settings": {"volume": None, "shuffle": 0, "repeat": "off"},
        }
        with open(_mk(self.tmp), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        store = QueueStore(_mkcfg(self.tmp), self.sp)
        try:
            self.assertIsNone(store.pos)
        finally:
            store.close()


class LockQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="q5-lq-")
        self.sp = _spy(self.tmp)

    def tearDown(self):
        os.system(f"rm -rf {self.tmp}")

    def test_future_version_locks_the_file(self):
        payload = {"version": 99, "items": [], "index": 0, "settings": {}}
        with open(_mk(self.tmp), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        with open(_mk(self.tmp), "rb") as fh:
            before = fh.read()

        store = QueueStore(_mkcfg(self.tmp), self.sp)
        try:
            self.assertTrue(store.version_locked,
                            "a future schema version must LOCK the writer")
            # mutations stay volatile (memory only) and are NEVER written
            # over the foreign file: the invariant is enforced at the
            # render boundary.
            store.replace(_items())
            store.set_phase(PHASE_PLAYING)
        finally:
            store.close()
        with open(_mk(self.tmp), "rb") as fh:
            self.assertEqual(fh.read(), before,
                         "the operator's file must be byte-identical")
        self.assertTrue(_facts(self.sp, "store_version_locked"))

    def test_corrupt_file_quarantines_and_starts_fresh(self):
        with open(_mk(self.tmp), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json !!")

        store = QueueStore(_mkcfg(self.tmp), self.sp)
        try:
            store.replace(_items())
            self.assertEqual(len(store.items), 3)
            store.close()
        finally:
            store.close()

        quarantined = sorted(glob.glob(
            os.path.join(self.tmp, "state.json.corrupt-*")))
        self.assertEqual(len(quarantined), 1,
                         "the corrupt original is RENAMED (evidence), "
                         "never destroyed in place")
        with open(quarantined[0], encoding="utf-8", errors="replace") as fh:
            self.assertIn("not json", fh.read())
        self.assertTrue(_facts(self.sp, "store_state_quarantined"))
        # and the next render REBUILT the canonical path as a healthy
        # file (the module's promise: quarantine, then rebuild).
        with open(_mk(self.tmp), encoding="utf-8") as fh:
            rebuilt = json.load(fh)
        self.assertEqual(rebuilt["version"], STORE_VERSION)
        self.assertEqual(len(rebuilt["items"]), 3)


class ConcurrencyZeroLossTests(unittest.TestCase):
    """THE headline A5 acceptance at the store level.

    Eight workers mutate the SAME store concurrently. Every mutation is
    one serialized RLock section that ends in a synchronous flush (dis-
    crete) or a single windowed kick (continuous), so the revision
    counter must advance by EXACTLY the number of applied mutations —
    not one less (lost update) and not one more (phantom) — and the
    end state/file must stay coherent.
    """

    OPS_PER_THREAD = 25

    def test_eight_workers_exactly_one_revision_per_mutation(self):
        tmp = tempfile.mkdtemp(prefix="q5-race-")
        sp = _spy(tmp)
        store = QueueStore(_mkcfg(tmp, win=0.02), sp)
        failures: "list" = []

        def worker(tid: int):
            try:
                for i in range(self.OPS_PER_THREAD):
                    k = (tid * 7 + i) % 6
                    if k == 0:
                        store.replace(_items()[: 1 + (i % 3)])
                    elif k == 1:
                        store.append([QueueItem(id=f"s-{tid}-{i}",
                                               title=f"T{tid}.{i}")])
                    elif k == 2:
                        store.step(+1 if i % 2 else -1)
                    elif k == 3:
                        store.set_phase([PHASE_PLAYING, PHASE_PAUSED][i % 2])
                    elif k == 4:
                        store.set_position(float((i % 50) + tid))
                    else:
                        store.touch_settings(volume=(i * 3 + tid) % 101)
            except Exception as exc:
                failures.append((tid, type(exc).__name__, str(exc)))

        threads = [threading.Thread(target=worker, args=(t,), name=f"w{t}")
                   for t in range(8)]
        initial_seq = store.seq
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        store.close()

        self.assertEqual(failures, [], f"mutations raised: {failures}")
        expected = 8 * self.OPS_PER_THREAD
        self.assertEqual(store.seq - initial_seq, expected,
                         "EXACTLY one revision per mutation — zero lost "
                         f"(got {store.seq - initial_seq}, want {expected})")

        with open(_mk(tmp), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], STORE_VERSION)
        n = len(data["items"])
        idx = data["index"]
        self.assertTrue(idx is None or 0 <= idx < n,
                        "index coherence under concurrent steps")
        self.assertIn(data["phase"], (PHASE_PLAYING, PHASE_PAUSED,
                                      PHASE_STOPPED))
        os.system(f"rm -rf {tmp}")


class CoalescingWindowTests(unittest.TestCase):
    def test_sustained_burst_collapses_to_bounded_writes(self):
        tmp = tempfile.mkdtemp(prefix="q5-win-")
        sp = _spy(tmp)
        store = QueueStore(_mkcfg(tmp, win=0.5), sp)
        store.replace(_items())  # one discrete (sync) write
        base = store.writes      # baseline before the burst
        t0 = time.monotonic()
        for i in range(100):
            # each sample moves the position at least one STEP (5 s) so
            # EVERY one of them would dirty the snapshot — the window
            # (0.5 s) is what must keep the PHYSICAL writes bounded
            # (at most one per window, plus the shutdown drain).
            store.track_observed({"idle": False,
                                  "playback-status": "playing",
                                  "playlist-current-id": "song-a",
                                  "time-pos": 5.0 * i})
            time.sleep(0.003)
        elapsed = time.monotonic() - t0
        time.sleep(0.6)  # let the writer finish any final window
        store.close()    # the shutdown drain adds at most one more

        delta = store.writes - base
        bound = max(2, int(elapsed // 0.5) + 3)
        self.assertLessEqual(delta, bound,
                             f"{delta} physical writes over {elapsed:.2f}s "
                             f"(bound {bound}); the coalescing window must "
                             "collapse the burst")
        self.assertGreaterEqual(delta, 1)
        os.system(f"rm -rf {tmp}")


class ShutdownDrainTests(unittest.TestCase):
    def test_close_flushes_pending_window(self):
        tmp = tempfile.mkdtemp(prefix="q5-drain-")
        sp = _spy(tmp)
        store = QueueStore(_mkcfg(tmp, win=30.0), sp)
        store.replace(_items())  # sync write settles the baseline
        # continuous updates far OUTSIDE the 30 s window: they stay
        # pending in memory and must land with the shutdown DRAIN.
        store.track_observed({"idle": False,
                              "playback-status": "paused",
                              "playlist-current-id": "song-b",
                              "time-pos": 0.0})
        store.track_observed({"idle": False,
                              "playback-status": "paused",
                              "playlist-current-id": "song-b",
                              "time-pos": 12.5})
        with open(_mk(tmp), encoding="utf-8") as fh:
            before = json.load(fh)
        # the window has NOT rendered the pending observations: the
        # last physical write predates them (one window is all a kill
        # inside it can lose). A clean shutdown drains them instead.
        self.assertNotEqual(before.get("pos"), 12.5)
        store.close()
        with open(_mk(tmp), encoding="utf-8") as fh:
            after = json.load(fh)
        self.assertEqual(after.get("pos"), 12.5)
        self.assertEqual(after.get("phase"), PHASE_PAUSED)
        self.assertEqual(after.get("cur_track"), "song-b")
        os.system(f"rm -rf {tmp}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
