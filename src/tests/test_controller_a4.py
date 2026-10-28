"""A4 — PlaybackController: protocol + reconciliation, stubbed transport.

Pure unit tests (no socket, no subprocess): the controller is exercised
against recording stubs, so every assertion is a DIRECT statement about
the emitted JSON-IPC commands / store mutations — the strongest
possible proof that the /api/v1 translation layer is what the ADR says
it is. Timing-dependent behaviours (EOF walk, gapless spacing) live in
the C1 mock integration (test_api_m1).

Coverage (card A4 acceptance):
  * quota: id-tagged apply protocol, exact command order;
  * transport: next/prev/stop/pause/volume/shuffle/repeat/seek;
  * estado: engine->store reconciliation (id anchor), abstentions,
    persistence of agreements;
  * gapless: full-queue load + native playlist-next/prev (asserted as
    the ONLY commands the transport emits — no unload/reload path);
  * seek: clamp to the loaded duration.
"""

from __future__ import annotations

import unittest

from navidrome_remote.controller import (
    REPEAT_PLAN,
    PlaybackController,
)


# ------------------------------------------------------------------ stubs
class StubItem:
    def __init__(self, id_, title=""):
        self.id = id_
        self.title = title


class StubStore:
    def __init__(self, ids=("11", "22", "33"), index=0):
        self.items = [StubItem(i, i.upper()) for i in ids]
        self.index = index
        self.settings = {"volume": None, "shuffle": False, "repeat": "off"}
        self.pending_restore = True
        self.saved = 0

    def save(self):
        self.saved += 1

    def mark_selected(self, idx, persist=False):
        self.index = idx
        if persist:
            self.save()

    def find_by_id(self, item_id):
        want = str(item_id)
        for i, it in enumerate(self.items):
            if it.id == want:
                return i
        return None


class StubEngine:
    def __init__(self, alive=True, idle=True, count=0,
                 props=None, current_id=None):
        self.alive = alive
        self.cmds = []
        self._props = dict(props or {})
        self._props.setdefault("idle", idle)
        self._props.setdefault("playlist-count", count)
        if current_id is not None:
            self._props["playlist-current-id"] = current_id

    def cmd(self, *args):
        first = args[0]
        self.cmds.append(list(first) if isinstance(first, list) else list(args))

    def get_property(self, name):
        try:
            return self._props[name]
        except KeyError:
            raise RuntimeError("property-not-found: %s" % name)

    def get_properties(self, names):
        out = {}
        for n in names:
            out[n] = self.get_property(n)
        return out


class SilentSpy:
    def __init__(self):
        self.events = []

    def event(self, *a, **kw):
        self.events.append(("async", a, kw))

    def event_sync(self, *a, **kw):
        self.events.append(("sync", a, kw))


class StubCfg:
    nav_user = "sbc"
    nav_pass = "svc-pass-91-Q"
    upstream_base_url = "http://nas.local:4533/"
    api_token = None


def _ctl(ids=("11", "22", "33"), index=0, **eng_kw):
    store = StubStore(ids, index)
    eng = StubEngine(**eng_kw)
    spy = SilentSpy()
    return PlaybackController(StubCfg(), spy, eng, store), eng, store, spy


# ============================================================== protocol
class ApplyProtocolTests(unittest.TestCase):
    def test_exact_command_sequence(self):
        ctl, eng, store, spy = _ctl(index=0)
        store.settings["volume"] = 42
        self.assertTrue(ctl.apply_queue())
        self.assertFalse(store.pending_restore)
        # clear, add x3 (each id-tagged), play <start>, settings mirror
        heads = [c[0] for c in eng.cmds]
        self.assertEqual(heads[:1], ["playlist-clear"])
        self.assertEqual(heads[1:4], ["playlist-add"] * 3)
        self.assertEqual(eng.cmds[4][:2], ["playlist-play", 0])
        for i, c in enumerate(eng.cmds[1:4]):
            self.assertEqual(c[3], store.items[i].id,
                             "entry %d must be id-tagged" % i)
        tail = {c[1]: c[2] for c in eng.cmds if c[0] == "set_property"}
        self.assertEqual(tail, {"volume": 42.0, "shuffle": 0,
                                "loop-file": 0, "loop-playlist": 0})
        # credential invariant: the spy records NAMES and COUNTS only
        blob = repr(spy.events)
        self.assertNotIn("rest/stream", blob,
                         "stream URL must never reach the spy")
        self.assertNotIn("u=sbc", blob)
        self.assertNotIn("svc-pass", blob)
        self.assertIn("store_apply_ok", blob)

    def test_start_honours_persisted_index(self):
        ctl, eng, _, _ = _ctl(index=2)
        ctl.apply_queue()
        play = [c for c in eng.cmds if c[0] == "playlist-play"][0]
        self.assertEqual(play[1], 2)

    def test_empty_queue_is_trivial_success(self):
        ctl, eng, store, _ = _ctl(ids=())
        self.assertTrue(ctl.apply_queue())
        self.assertFalse(eng.cmds)
        self.assertFalse(store.pending_restore)

    def test_no_credentials_skips_without_commands(self):
        ctl, eng, _, spy = _ctl()
        ctl.cfg.nav_pass = ""
        self.assertFalse(ctl.apply_queue())
        self.assertFalse(eng.cmds)
        self.assertIn("store_apply_skipped", repr(spy.events))

    def test_partial_ipc_failure_keeps_receipt(self):
        class FlakyEngine(StubEngine):
            def cmd(self, *args):
                if isinstance(args[0], list) and args[0][0] == "playlist-add":
                    raise RuntimeError("boom")
                super().cmd(*args)

        store = StubStore()
        spy = SilentSpy()
        ctl = PlaybackController(StubCfg(), spy, FlakyEngine(), store)
        self.assertFalse(ctl.apply_queue())
        self.assertNotIn("store_apply_ok", repr(spy.events))
        self.assertIn("store_apply_failed", repr(spy.events))


# =============================================================== transport
class TransportCommandsTests(unittest.TestCase):
    def test_one_stock_command_per_action(self):
        ctl, eng, _, _ = _ctl()
        ctl.play("http://x/y")
        ctl.pause(True)
        ctl.stop()
        ctl.next()
        ctl.prev()
        ctl.select(3)
        ctl.volume(55)
        self.assertEqual(
            eng.cmds,
            [["loadfile", "http://x/y", "replace"],
             ["set_property", "pause", True],
             ["stop"],
             ["playlist-next"],
             ["playlist-prev"],
             ["playlist-play", 3],
             ["set_property", "volume", 55.0]],
            "gapless: the transport may ONLY use native hand-offs — "
            "no unload/reload path exists in the controller")

    def test_shuffle_writes_the_stock_flag(self):
        ctl, eng, _, _ = _ctl()
        ctl.shuffle(1)
        self.assertEqual(eng.cmds, [["set_property", "shuffle", 1]])

    def test_repeat_pairs_single_source(self):
        ctl, eng, _, _ = _ctl()
        ctl.repeat("one")
        self.assertEqual(eng.cmds, [
            ["set_property", "loop-file", -1],
            ["set_property", "loop-playlist", 0]])
        eng.cmds.clear()
        ctl.repeat("all")
        self.assertEqual(eng.cmds, [
            ["set_property", "loop-file", 0],
            ["set_property", "loop-playlist", -1]])

    def test_repeat_invalid_raises(self):
        ctl, _, _, _ = _ctl()
        with self.assertRaises(ValueError):
            ctl.repeat("banana")

    def test_repeat_table(self):
        self.assertEqual(REPEAT_PLAN["off"], (0, 0))
        self.assertEqual(REPEAT_PLAN["track"], (-1, 0))
        self.assertEqual(REPEAT_PLAN["one"], (-1, 0))
        self.assertEqual(REPEAT_PLAN["playlist"], (0, -1))
        self.assertEqual(REPEAT_PLAN["all"], (0, -1))


class SeekClampTests(unittest.TestCase):
    def test_clamps_to_loaded_duration(self):
        ctl, eng, _, _ = _ctl(props={"duration": 30.0})
        self.assertEqual(ctl.seek(42), 30.0)
        self.assertEqual(eng.cmds, [["seek", 30.0, "absolute"]])

    def test_inside_range_verbatim(self):
        ctl, eng, _, _ = _ctl(props={"duration": 300.0})
        self.assertEqual(ctl.seek(42), 42.0)
        self.assertEqual(eng.cmds, [["seek", 42.0, "absolute"]])

    def test_negative_target_lands_at_zero(self):
        ctl, eng, _, _ = _ctl(props={"duration": 300.0})
        self.assertEqual(ctl.seek(-5), 0.0)
        self.assertEqual(eng.cmds, [["seek", 0.0, "absolute"]])

    def test_no_duration_reported_sends_verbatim(self):
        ctl, eng, _, _ = _ctl()
        self.assertEqual(ctl.seek(42), 42.0)
        self.assertEqual(eng.cmds, [["seek", 42.0, "absolute"]])


# ============================================================= reconcile
class ReconciliationTests(unittest.TestCase):
    def test_store_follows_engine_id(self):
        ctl, _, store, _ = _ctl(index=0)
        res = ctl.reconcile({"idle": False, "playlist-current-id": "33"})
        self.assertTrue(res.changed)
        self.assertEqual(store.index, 2)
        self.assertEqual(store.saved, 1, "agreement must persist")

    def test_same_id_is_no_write(self):
        ctl, _, store, _ = _ctl(index=2)
        res = ctl.reconcile({"idle": False, "playlist-current-id": "33"})
        self.assertFalse(res.changed)
        self.assertEqual(store.saved, 0,
                         "matching state must not spam the disk")

    def test_idle_abstains(self):
        ctl, _, store, _ = _ctl(index=0)
        res = ctl.reconcile({"idle": True, "playlist-current-id": "33"})
        self.assertFalse(res.changed)
        self.assertEqual(store.index, 0)
        self.assertEqual(store.saved, 0)

    def test_unknown_id_abstains(self):
        ctl, _, store, _ = _ctl(index=0)
        res = ctl.reconcile({"idle": False, "playlist-current-id": "zzz"})
        self.assertFalse(res.changed)
        self.assertEqual(store.index, 0,
                         "the store keeps holding the intent (A3) until the "
                         "engine speaks an id we recognise")

    def test_absent_idle_prop_treated_as_unknown(self):
        # a hostile engine that omits `idle` must make us ABSTAIN, not
        # flip state on a guess
        ctl, _, store, _ = _ctl(index=0)
        res = ctl.reconcile({"playlist-current-id": "33"})
        self.assertFalse(res.changed)

    def test_active_with_track_truth_table(self):
        ctl, eng, _, _ = _ctl(alive=False, idle=False, count=3)
        self.assertFalse(ctl.active_with_track(), "dead engine")
        ctl, eng, _, _ = _ctl(alive=True, idle=True, count=3)
        self.assertFalse(ctl.active_with_track(), "idle engine")
        ctl, eng, _, _ = _ctl(alive=True, idle=False, count=0)
        self.assertFalse(ctl.active_with_track(), "empty playlist")
        ctl, eng, _, _ = _ctl(alive=True, idle=False, count=3)
        self.assertTrue(ctl.active_with_track())

    def test_snapshot_batches_core_plus_soft_probe(self):
        seen = {}

        class Eng:
            alive = True

            def get_property(self, name):
                return {"playlist-current-id": "33"}.get(name)

            def get_properties(self, names):
                seen["names"] = list(names)
                return {n: {"idle": False,
                            "playback-status": "playing",
                            "time-pos": 1.5,
                            "media-title": "33",
                            "shuffle": 1,
                            "loop-file": 0,
                            "loop-playlist": -1,
                            "volume": 55,
                            "duration": 180.0,
                            "playlist-index": 2,
                            "playlist-count": 3}[n] for n in names}

        store = StubStore(index=0)
        ctl = PlaybackController(StubCfg(), SilentSpy(), Eng(), store)
        snap = ctl.snapshot()
        self.assertIn("duration", seen["names"],
                      "core set must batch the duration (clamp source)")
        self.assertIn("playlist-count", seen["names"])
        self.assertNotIn("playlist-current-id", seen["names"],
                         "the reconciliation anchor is a SOFT probe — "
                         "its absence must never take /status down")
        self.assertEqual(snap["playlist-current-id"], "33")
        self.assertEqual(store.index, 2,
                         "snapshot must leave the store reconciled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
