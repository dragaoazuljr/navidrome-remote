"""A6 — double-intent absorption in the /api/v1 contract (unit level).

The browser-level behaviour is covered by suite/bridge_e2e.py T7 (real
Chrome, real production pipeline). These tests pin the SERVER-SIDE
commutativity without a browser, so the guarantee stays in the fast
suite:

  * same-id /play is an echo — it never flattens a seeded deck;
  * /next -> /play {its target} : the play is the transport's echo
    (deck survives intact);
  * /play {adjacent} -> /next   : whichever member lands first, a
    SINGLE gesture ends on the designated position without stopping
    the engine or overshooting; the deferred play either stands down
    (deck intact) or executes and the late step folds to a no-op
    (both end on the same track);
  * an UNMATCHED /play keeps the A3 contract exactly (a non-destructive
    load — the seeded deck survives intact).
"""

from __future__ import annotations

import asyncio
import json
import unittest

from .test_api_m1 import M1Service, _run, SESSION_QS

ITEMS = [{"id": "S%d" % (i + 1), "title": "Song %d" % (i + 1)}
         for i in range(3)]


def _qsize(s: dict) -> "object | None":
    q = s.get("queue") or {}
    return q.get("size", q.get("length"))


class _World:
    """One booted service + convenience verbs (mirrors the M1 harness)."""

    def __init__(self):
        self.svc = M1Service()

    async def setup(self) -> None:
        await self.svc.start()

    async def teardown(self) -> None:
        # awaited INSIDE the running loop (no nested _run)
        await self.svc.stop()

    async def seed(self) -> None:
        r = await self.svc.post_json(
            "/api/v1/queue",
            {"items": ITEMS, "mode": "replace"})
        assert r.status == 200, (r.status, r.text)

    async def play(self, sid: str) -> "tuple[int, dict]":
        r = await self.svc.post_json("/api/v1/play", {"id": sid})
        return r.status, json.loads(r.text)

    async def step(self, route: str) -> "tuple[int, dict]":
        r = await self.svc.post_json(route, {})
        return r.status, json.loads(r.text)

    async def status(self) -> dict:
        r = await self.svc.get("/api/v1/status" + SESSION_QS)
        assert r.status == 200, (r.status, r.text)
        return json.loads(r.text)


class A6DoubleIntentAbsorptionTests(unittest.TestCase):
    def test_same_id_play_is_an_echo_and_deck_survives(self):
        async def go():
            w = _World()
            await w.setup()
            try:
                await w.seed()
                st, obj = await w.play("S1")  # == current
                self.assertEqual(st, 200)
                self.assertEqual(obj.get("echo"), "same-id")
                s = await w.status()
                self.assertEqual((s.get("play") or {}).get("id"), "S1")
                self.assertIn(_qsize(s), (3, "3"))
            finally:
                await w.teardown()
        _run(go())

    def test_step_then_twin_play_folds_into_same_id(self):
        async def go():
            w = _World()
            await w.setup()
            try:
                await w.seed()
                st, _obj = await w.step("/api/v1/next")
                self.assertEqual(st, 200)
                # after the step, the twin's target == current, so the
                # STRICTER same-id rule folds it (no write at all)
                st, obj = await w.play("S2")
                self.assertEqual(st, 200)
                self.assertEqual(obj.get("echo"), "same-id")
                s = await w.status()
                self.assertEqual((s.get("play") or {}).get("id"), "S2")
                self.assertIn(_qsize(s), (3, "3"))
            finally:
                await w.teardown()
        _run(go())

    def test_recent_step_suppresses_stale_forward_play(self):
        async def go():
            w = _World()
            await w.setup()
            try:
                await w.seed()
                st, _obj = await w.step("/api/v1/next")  # S1 -> S2
                self.assertEqual(st, 200)
                # a stale twin computed from the OLD snapshot points ONE
                # ahead of the post-step position: next-echo absorbs it
                st, obj = await w.play("S3")
                self.assertEqual(st, 200)
                self.assertEqual(obj.get("echo"), "next-echo")
                s = await w.status()
                # no overshoot: the engine stays on the step's target
                self.assertEqual((s.get("play") or {}).get("id"), "S2")
                self.assertIn(_qsize(s), (3, "3"))
            finally:
                await w.teardown()
        _run(go())

    def test_play_and_next_race_lands_exactly_once(self):
        async def go():
            w = _World()
            await w.setup()
            try:
                await w.seed()

                async def fire_play():
                    st, obj = await w.play("S2")
                    self.assertEqual(st, 200)
                    return obj

                async def fire_next():
                    st, obj = await w.step("/api/v1/next")
                    self.assertEqual(st, 200)
                    return obj

                objs = await asyncio.gather(fire_play(), fire_next())
                s = await w.status()
                # whatever the scheduler interleaved, ONE gesture ends
                # on the designated position — no stop, no overshoot.
                self.assertEqual((s.get("play") or {}).get("id"), "S2")
                self.assertFalse(s.get("idle"))
                modes = {o.get("echo") for o in objs}
                if "superseded-by-" in str(modes):
                    # the deferred play stood down: the deck is intact
                    self.assertIn(_qsize(s), (3, "3"))
                else:
                    # the play ran as a structural replacement (deadline)
                    # and the late step folded: single-entry deck, S2.
                    self.assertIn(_qsize(s), (1, "3"))
            finally:
                await w.teardown()
        _run(go())

    def test_unmatched_play_keeps_a3_contract(self):
        async def go():
            w = _World()
            await w.setup()
            try:
                await w.seed()
                st, obj = await w.play("S9")  # not in the deck, not adjacent
                self.assertEqual(st, 200)
                self.assertIsNone(obj.get("echo"))
                s = await w.status()
                # A3 semantics: an off-deck load is NON-DESTRUCTIVE —
                # it plays the track without truncating the deck.
                self.assertIn(_qsize(s), (3,))
                self.assertEqual((s.get("play") or {}).get("id"), "S9")
            finally:
                await w.teardown()
        _run(go())
