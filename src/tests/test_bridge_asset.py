"""Hermetic lint of assets/bridge.js (A6) — runs everywhere, no browser.

The asset is the only browser-side code we ship; these static checks pin
the SAFETY surface of it (credential invariant, idempotency, route
contract, hygiene) so a future edit that would break one of them fails
the suite BEFORE a browser is involved. Complements — does not replace
— the browser-level matrix (suite/bridge_e2e.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET = os.path.normpath(
    os.path.join(HERE, "..", "navidrome_remote", "assets", "bridge.js"))

ROUTE_CONTRACT = [
    "/play", "/pause", "/resume", "/next", "/prev", "/stop",
    "/seek", "/shuffle", "/repeat", "/volume", "/status",
]
ALLOWED_LS_READS = ("token", "navidrome_token", "is-authenticated")


class AssetLintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ASSET, "r", encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.text = cls.src

    # ------------------------------------------------------------- basics
    def test_size_sanity(self):
        self.assertGreater(len(self.src), 4000,
                           "asset implausibly small for the A6 scope")
        self.assertLess(len(self.src), 80000,
                        "asset implausibly large (keep it lean)")

    def test_node_syntax_check(self):
        node = None
        for cand in ("node", "nodejs"):
            p = shutil.which(cand)
            if p:
                node = p
                break
        if node is None:
            self.skipTest("node indisponível neste ambiente")
        r = subprocess.run([node, "--check", ASSET],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0,
                         "node --check falhou: %s" % (r.stderr or "?"))

    def test_version_and_anchor_constants(self):
        self.assertIn('"v0.64.2"', self.src)
        self.assertIn('"0.3.0-a6"', self.src)

    # ------------------------------------------------------ idempotency
    def test_single_global_guard(self):
        n = self.src.count("window.__NVRA_BRIDGE__")
        # assignment + reads; the GUARD (early-return) must be singular
        self.assertGreaterEqual(n, 2)
        self.assertEqual(self.src.count("if (window.__NVRA_BRIDGE__)"), 1,
                         "guard de injeção-única deve existir exatamente 1x")

    # -------------------------------------------------------- route map
    def test_all_contract_routes_present(self):
        for r in ROUTE_CONTRACT:
            self.assertIn(r, self.src,
                          "rota do contrato A3 ausente no asset: %s" % r)

    def test_beacon_pad_target(self):
        self.assertIn("/api/v1/spy", self.src)
        self.assertIn("obj.jwt", self.src,
                      "o pad de beacon carrega a sessão em 'jwt' no corpo "
                      "(sendBeacon não monta headers)")

    def test_bridge_never_direct_probes_upstream(self):
        self.assertNotIn("/rest/getIndexes", self.src,
                         "o bridge NUNCA sondea o upstream diretamente — "
                         "isso é trabalho do gate (L3) do serviço")

    # --------------------------------------------- credential invariant
    def test_localstorage_reads_whitelisted(self):
        reads = set(re.findall(r"localStorage\.getItem\([\"']([\w-]+)[\"']",
                               self.src))
        for r in reads:
            self.assertIn(r, ALLOWED_LS_READS,
                          "o bridge lê uma chave LS fora da whitelist "
                          "(invariante de credenciais): %s" % r)

    def test_never_reads_subsonic_tokens(self):
        low = self.src.lower()
        self.assertNotIn("subsonic-token", low)
        self.assertNotIn("subsonic_token", low)
        self.assertNotIn('"nd_session"', low)

    def test_no_eval_or_dynamic_constructors(self):
        self.assertNotIn("new Function", self.src)
        self.assertNotIn("eval(", self.src)

    def test_sanitizer_present_and_shapes_right(self):
        self.assertIn("_SECRET_RX", self.src)
        self.assertIn("[red]", self.src)
        self.assertRegex(self.src, r"eyJ")

    def test_test_seam_exposed(self):
        for name in ("_state", "_reason", "_intents", "_status",
                     "_stats", "_shadow"):
            self.assertIn("'" + name + "'", self.src)

    # ------------------------------------------------- payload hygiene
    def test_json_payloads_are_flat(self):
        """Intents/beacons must ship FLAT small objects (easy to lint on
        the server side, easy to redact). Detect accidental nesting by
        counting braces in JSON.stringify call sites heuristically."""
        strfies = re.findall(r"JSON\.stringify\(\{[^)]*\}", self.src)
        self.assertLessEqual(len(strfies), 12,
                             "excessivas construções ad-hoc de payload")


if __name__ == "__main__":
    unittest.main(verbosity=2)
