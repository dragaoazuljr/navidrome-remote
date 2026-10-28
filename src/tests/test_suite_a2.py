#!/usr/bin/env python3
"""test_suite_a2.py — unidades da lógica da suíte A2 (offline, stdlib).

Cobre (sem rede):
  * ws_probe.classify — o coração da cláusula B(i): distingue
      GREEN (101 atravessa), MIRRORED (upstream recusa, nós espelhamos
      sem romper — red *fato*, não *nosso*), BROKEN (divergência
      proxy×direct — vermelho NOSO) e INCONCLUSIVE.
  * cors_map.norm_cell/cell_diff/pair_channels — a métrica de
      paridade directo×proxy (cláusula A): campos voláteis fora,
      divergência por campo.
  * cdp.redact/_select_headers — invariante de credenciais (o dump do
      Tab nunca leva valor de cookie/sal/JWT).

Execução (src/ como top-level, como a descoberta padrão do repo):
  PYTHONPATH="$PWD" python3 -m unittest tests.test_suite_a2 -v
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, ".."))
SUITEDIR = os.path.join(SRC, "suite")
sys.path.insert(0, SUITEDIR)
sys.path.insert(0, SRC)

import cors_map as cm  # noqa: E402
import ws_probe as wp  # noqa: E402
import cdp  # noqa: E402


# ================================================================ ws_probe
def P(result, status, detail=""):
    return {"result": result, "status": status, "detail": detail,
            "hdrs": {}, "body_len": 0, "echo": None}


class ClassifyTests(unittest.TestCase):
    def test_green_single_channel(self):
        r = wp.classify("/ws", P("101", 101), None)
        self.assertEqual(r["cls"], "green")
        self.assertIn("101", r["verdict"])

    def test_green_both_channels(self):
        r = wp.classify("/ws", P("101", 101), P("101", 101))
        self.assertEqual(r["cls"], "green")
        self.assertIn("dois canais", r["verdict"])

    def test_mirrored_refusal_is_not_our_red(self):
        r = wp.classify("/events",
                        P("HTTP-404", 404), P("HTTP-404", 404))
        self.assertEqual(r["cls"], "mirrored")
        self.assertIn("SEM ROMPIMENTO", r["verdict"].upper())

    def test_broken_when_diverged(self):
        r = wp.classify("/sse", P("HTTP-426", 426), P("HTTP-404", 404))
        self.assertEqual(r["cls"], "broken")
        self.assertIn("VERMELHO", r["verdict"].upper())
        self.assertIn("426", r["verdict"])

    def test_inconclusive_on_conn_error(self):
        r = wp.classify("/ws", {"result": "err", "status": None,
                                "detail": "conn-OSError"}, None)
        self.assertEqual(r["cls"], "inconclusive")

    def test_non101_without_direct_is_fact(self):
        r = wp.classify("/app/ws", P("HTTP-400", 400), None)
        self.assertEqual(r["cls"], "red-facto")


# ============================================================== cors_map
class NormCellTests(unittest.TestCase):
    HDRS_M = {"access-control-allow-origin": "http://a",
              "vary": "Origin", "date": "Mon, 28 Oct 2026 10:00:00 GMT"}
    HDRS_D = {"access-control-allow-origin": "http://b",
              "vary": "Origin", "date": "Mon, 28 Oct 2026 11:00:00 GMT"}

    def test_identical_cells_no_diff(self):
        a = cm.norm_cell(200, self.HDRS_M, 100)
        b = cm.norm_cell(200, self.HDRS_M, 90)
        self.assertEqual(cm.cell_diff(a, b), [])

    def test_acao_divergence_detected(self):
        a = cm.norm_cell(200, self.HDRS_M, 100)
        b = cm.norm_cell(200, self.HDRS_D, 100)
        d = cm.cell_diff(a, b)
        self.assertTrue(any("access-control-allow-origin" in x
                            for x in d))

    def test_volatiles_excluded(self):
        a = cm.norm_cell(200, {**self.HDRS_M, "date": "x"}, 100)
        b = cm.norm_cell(200, {**self.HDRS_M, "date": "y"}, 100)
        self.assertEqual(cm.cell_diff(a, b), [],
                         "header volátil (Date) contaminou o pareamento")

    def test_status_differs(self):
        a = cm.norm_cell(200, {}, 0)
        b = cm.norm_cell(302, {}, 0)
        self.assertTrue(any("status" in x for x in cm.cell_diff(a, b)))


class PairChannelsTests(unittest.TestCase):
    def _cell(self, route, ctx, origin, acr, chan, status,
              acao=None):
        hdrs = {}
        if acao:
            hdrs["access-control-allow-origin"] = acao
        base = {
            "route": route, "ctx_effective": ctx, "origin": origin,
            "acr_method": acr, "channel_label": chan, "method": "GET",
            "status": status, "reason": "OK", "body_len": 10,
            "body_head": "x", "headers": hdrs,
            "normalized": cm.norm_cell(status, hdrs, 10),
        }
        return base

    def test_pair_mirrored_and_diverged(self):
        cells = [
            self._cell("/rest/ping", "anon", None, None, "direct", 401),
            self._cell("/rest/ping", "anon", None, None, "proxy", 401),
            self._cell("/rest/ping", "anon", "http://x:1", "GET",
                       "direct", 200, acao="*"),
            self._cell("/rest/ping", "anon", "http://x:1", "GET",
                       "proxy", 200, acao="http://other"),
        ]
        pairs = cm.pair_channels(cells)
        self.assertEqual(len(pairs), 2)
        by_acr = {p["key"]["acr"]: p for p in pairs}
        self.assertTrue(by_acr["<get>"]["mirror"])
        self.assertFalse(by_acr["GET"]["mirror"])
        self.assertTrue(any("access-control-allow-origin" in x
                            for x in by_acr["GET"]["diffs"]))

    def test_missing_channel_yields_no_pair(self):
        cells = [self._cell("/x", "anon", None, None, "direct", 200)]
        self.assertEqual(cm.pair_channels(cells), [])


# =================================================================== cdp
class CredentialInvariantTests(unittest.TestCase):
    def test_cookie_value_scrubbed(self):
        out = cdp._select_headers({"Set-Cookie": "nd_sess=TOPSECRET; HttpOnly",
                                   "Accept": "text/html"})
        blob = "|".join("%s=%s" % (k, v) for k, v in out)
        self.assertIn("nd_sess", blob)
        self.assertNotIn("TOPSECRET", blob)

    def test_jwt_and_salt_masked(self):
        tok = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
               "eyJ1c2VyIjoiYWxpY2UifQ.deadbeefdeadbeef")
        r = cdp.redact(tok)
        self.assertNotIn(tok, r)
        salt = "A" * 44
        r2 = cdp.redact("ping?s=%s" % salt)
        self.assertNotIn(salt, r2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
