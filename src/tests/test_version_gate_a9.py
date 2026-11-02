"""A9 (ADR-007) — version-compatibility gate: unit locks.

Three layers, all hermetic (no browser, no network):

  1. DECISION MATRIX — a Python mirror of the bridge's classifyUi()
     decision table. The JS order is contractual (drift is decided
     FROM the declaration BEFORE the feature probe runs), so the mirror
     documents the semantics and the asset lint (test_bridge_asset)
     pins the physical order in the source. The mirrored contract is
     the EMPIRICAL one of the anchored generation (live v0.64.2,
     characterized 2026-11-01): a __APP_CONFIG__ carrier installed at
     runtime (object or serialized string) exposing a parseable
     version; version bases drop vendor prefix and (-build/-tag).
     Together they catch a "someone reordered the classifier"
     regression on both sides.

  2. STREAM RX CROSS-LOCK — the asset's official-channel regex is
     extracted from the source and re-implemented here in Python: it
     MUST match the anchor's delivery shape and MUST NOT match the
     foreign channel of a hypothetical next generation (the exact
     shape the E2E fixture exercises). If someone widens the RX to
     "match any stream* URL", the broken-hook scenario stops working
     silently — this test is the tripwire.

  3. FIXTURE CONTRACTS — main.html (anchor generation: publishes
     __APP_CONFIG__.version), drift.html (declared foreign version)
     and broke.html (A9: same-shaped object WITHOUT the version key —
     the broken hook — delivering audio through /rest/stream2).
"""

from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(
    os.path.join(HERE, "..", "navidrome_remote", "assets", "bridge.js"))
FXDIR = os.path.normpath(
    os.path.join(HERE, "..", "suite", "fixtures", "bridge_ui"))
ANCHOR = "0.64.2"

# the JS regex as written in the asset (one source of truth: we extract
# IT, then re-implement the same pattern in Python below)
JS_RX_LIT = re.escape(
    r"/(?:^|\/)(?:rest\/)?stream(?:$|[?#])/")


def _base(x: "str | None") -> str:
    """Comparison base (mirror of the JS verBase): drop a leading
    vendor 'v', then everything after '(' (build) or '-' (tag)."""
    s = str(x or "")
    if s[:1] in ("v", "V"):
        s = s[1:]
    return s.split("(")[0].split("-")[0].strip()


def _anchor_match(v: "str | None") -> bool:
    """Mirror of the asset's anchorMatch (A9/ADR-007): normalized-base
    comparison; empty -> informational True."""
    if not v:
        return True
    return _base(v) == _base(ANCHOR)


def _page_version(carrier) -> "str | None":
    """Mirror of the bridge's pageVersion(): an object with a string
    `version`, or a serialized string bearing a version field."""
    if carrier is None:
        return None
    if isinstance(carrier, dict):
        ver = carrier.get("version")
        return ver if isinstance(ver, str) and ver else None
    if isinstance(carrier, str):
        m = re.search(r"['\"]?version['\"]?\s*[:=]\s*['\"]([^'\"]+)", carrier)
        return m.group(1) if m else None
    return None


def _probe_features(carrier) -> bool:
    """Mirror of the A9 feature probe: the __APP_CONFIG__ carrier
    EXISTS and yields a parseable version (empirical contract: the
    carrier is installed at runtime and may be an object OR a
    serialized string)."""
    return _page_version(carrier) is not None


def classify_mirror(carrier, declared_override: "str | None" = None) -> str:
    """Mirror of the asset's classifyUi() — SAME order as the source:
    drift (from the DECLARATION, which in the JS is exactly what
    pageVersion reads from the carrier) is decided before the feature
    probe; a verifiable contract yields known; otherwise unknown
    (broken hook: never intercept)."""
    declared = declared_override if declared_override is not None \
        else _page_version(carrier)
    if declared and not _anchor_match(declared):
        return "drift"
    if _probe_features(carrier):
        return "known"
    return "unknown"


class ClassifyMatrixTests(unittest.TestCase):
    """The decision table is the gate's contract. Each row is a
    (declared version, config object?, version-string?) situation and
    the EXPECTED class the bridge must produce."""

    ROWS = [
        # (carrier, declared_override, expected, note)
        ({"version": "0.64.2"}, None, "known",
         "anchor generation (main.html)"),
        ({"version": "v0.64.2"}, None, "known",
         "anchor with vendor 'v' prefix"),
        ({"version": "0.64.2 (10114574)"}, None, "known",
         "LIVE v0.64.2: full serverVersion — normalized base matches"),
        ('{"version": "0.64.2 (10114574)", "flavor": "web"}', None, "known",
         "LIVE v0.64.2: serialized STRING carrier (empirical contract)"),
        ({"version": "0.65.0"}, None, "drift",
         "T13: foreign declared version — declared outranks features"),
        ({"version": "0.65.0 (20240901)"}, None, "drift",
         "foreign generation with build suffix — still drift"),
        ({"build": "0.65.0-rc1"}, "0.70.1", "drift",
         "A9 corner: declaration still wins when the contract breaks"),
        ({"build": "0.65.0-rc1"}, None, "unknown",
         "A9 broken hook (broke.html): renamed key, no parsable version"),
        ({}, None, "unknown",
         "A9: carrier present but empty — contract unverifiable"),
        ("0.64.2", None, "unknown",
         "A9: bare value, no version FIELD in the carrier"),
        (None, None, "unknown",
         "A9: no carrier at all (hook absent)"),
        ({"version": "0.64.2"}, "0.99.9", "drift",
         "declaration override outranks a healthy contract (lock)"),
    ]

    def test_matrix(self):
        for carrier, declared, expected, _note in self.ROWS:
            with self.subTest(
                    declared=str(declared)[:20],
                    carrier=type(carrier).__name__):
                self.assertEqual(
                    classify_mirror(carrier, declared), expected)

    def test_ver_base_normalizes_live_forms(self):
        self.assertEqual(_base("0.64.2 (10114574)"), "0.64.2")
        self.assertEqual(_base("v0.64.2"), "0.64.2")
        self.assertEqual(_base("0.65.0-rc1"), "0.65.0")
        self.assertFalse(_anchor_match("0.65.0"))
        self.assertTrue(_anchor_match("0.64.2 (10114574)"))
        self.assertTrue(_anchor_match(None))

    def test_js_source_order_matches_mirror(self):
        with open(SRC, "r", encoding="utf-8") as fh:
            src = fh.read()
        # the physical order in the JS: drift-return BEFORE the probe
        # runs (probeFeatures() is called for the verdict only after
        # the declaration check); unknown is the terminal fallthrough.
        i_decl = src.index("function classifyUi()")
        body = src[i_decl:i_decl + 400]
        self.assertLess(body.find("!anchorMatch"), body.find("probeFeatures"),
                        "ordem invertida: a declaracao precede o probe")
        self.assertLess(body.find("\"drift\""),
                        body.rindex("\"unknown\""))


class StreamRxLockTests(unittest.TestCase):
    def test_rx_literal_is_the_documented_one(self):
        with open(SRC, "r", encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(JS_RX_LIT, src)
        self.assertIsNotNone(m, "o RX do canal oficial mudou de forma — "
                                "rever o contrato (ADR-007)")

    def test_rx_python_mirror_accepts_official_rejects_foreign(self):
        rx = re.compile(r"(?:^|/)(?:rest/)?stream(?:$|[?#])")
        official = [
            "/rest/stream?id=S1&u=u&t=t&s=s&v=1.12.1&c=navidrome",
            "/stream?id=S1&u=u",
            "//cdn.example.org/rest/stream?id=9",
        ]
        foreign = [
            "/rest/stream2?id=B1&u=fxt_user&t=a&s=b&v=2.0.0&c=navidrome2",
            "/media/streamer.mp3",
            "/rest/streaming/queue",
            "data:audio/x-wav;base64,AAA",
            "/api/v1/media/xyz",
        ]
        for u in official:
            self.assertTrue(rx.search(u), "RX deveria aceitar: %s" % u)
        for u in foreign:
            self.assertFalse(rx.search(u),
                             "RX NUNCA deveria aceitar (gancho partido): %s" % u)


class FixtureContractTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        with open(os.path.join(FXDIR, name), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_main_html_publishes_anchor_version(self):
        txt = self._read("main.html")
        # the real SPA publishes an UNSQUOTED JS key: {version: "..."}
        m = re.search(
            r"__APP_CONFIG__\s*=\s*\{[^}]*\bversion['\"]?\s*:\s*['\"]([^'\"]+)['\"]",
            txt, re.S)
        self.assertIsNotNone(m, "main.html tem que publicar .version")
        self.assertTrue(_anchor_match(m.group(1)),
                        "main.html tem que casar a ancora normalizada")

    def test_drift_html_declares_foreign_version(self):
        txt = self._read("drift.html")
        m = re.search(
            r"__APP_CONFIG__\s*=\s*\{[^}]*\bversion['\"]?\s*:\s*['\"]([^'\"]+)['\"]",
            txt, re.S)
        self.assertIsNotNone(m, "drift.html tem que declarar uma versao")
        self.assertFalse(_anchor_match(m.group(1)),
                         "drift.html tem que ser ESTRANGEIRA a ancora")

    def test_broke_html_has_no_version_key_but_keeps_shape(self):
        txt = self._read("broke.html")
        self.assertIn("__APP_CONFIG__", txt)
        mobj = re.search(r"__APP_CONFIG__\s*=\s*(\{.*?\})", txt, re.S)
        self.assertIsNotNone(mobj)
        blk = mobj.group(1)
        self.assertIsNone(
            re.search(r"\bversion['\"]?\s*:", blk),
            "broke.html NAO pode ter a chave version (isso e o gancho "
            "partido)")
        self.assertIsNotNone(
            re.search(r"\bbuild['\"]?\s*:\s*['\"]([^'\"]+)['\"]", blk),
            "broke.html publica o identificador sob outra chave "
            "(a geracao seguinte renomeou o contrato)")
        # a sessao continua presente (sessao presente NUNCA justifica
        # tocar uma geracao nao verificavel)
        self.assertIn('setItem("token"', txt)
        # o player toca num CANAL ESTRANHO a RX oficial da ancora
        mchan = re.search(r'/rest/(stream2\S*)', txt)
        self.assertIsNotNone(mchan,
                             "broke.html tem que entregar som via /rest/stream2")
        url = mchan.group(1).split("&")[0]
        self.assertFalse(re.search(r"(?:^|/)(?:rest/)?stream(?:$|[?#])", url),
                         "o canal da geracao partica nunca pode casar com "
                         "o RX oficial da ancora")

    def test_adr_recorded(self):
        adr = os.path.join(
            HERE, "..", "docs", "decisions",
            "2026-11-01-adr-007-version-compatibility-gate.md")
        self.assertTrue(os.path.exists(adr),
                        "ADR-007 ausente (a decisao do teto/contract "
                        "tem que estar registrada)")
        with open(adr, "r", encoding="utf-8") as fh:
            self.assertGreater(len(fh.read()), 1500,
                               "ADR-007 insuficiente (<1.5kB)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
