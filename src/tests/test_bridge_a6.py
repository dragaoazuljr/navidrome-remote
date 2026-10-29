"""Browser-level acceptance (A6) as a unittest entry point.

Wraps suite/bridge_e2e.py so the matrix runs inside the normal test
discovery (dev-ws). Without a Chrome/Chromium binary the test SKIPs
(the hermetic lint in test_bridge_asset.py still guards the asset; the
matrix itself needs a real browser, by design — QA drives it the same
way against the real LXC for the anchor verification).

The full matrix takes minutes (real headless Chrome + service + mock
engine); run it directly for iteration:
  python3 suite/bridge_e2e.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE_DIR = os.path.normpath(os.path.join(HERE, "..", "suite"))
sys.path.insert(0, SUITE_DIR)

import bridge_e2e  # noqa: E402  # pyright: ignore[reportMissingImports]


def _have_chrome() -> bool:
    return bridge_e2e.find_chrome() is not None


@unittest.skipUnless(_have_chrome(),
                     "Chrome/Chromium não presente (matriz browser-level "
                     "fica a cargo do QA/dev-ws; lint hermético cobre o "
                     "asset)")
class BridgeE2ETests(unittest.TestCase):
    def test_full_matrix(self):
        import asyncio

        class Args:
            scenario = ""
            chrome = ""
            report_dir = ""
            keep_going = True
        loop = asyncio.new_event_loop()
        try:
            rc = loop.run_until_complete(
                bridge_e2e._amain(Args()))
        finally:
            loop.close()
        self.assertEqual(rc, 0,
                         "matriz A6 terminou com falha (ver "
                         "docs/reports/a6-* e o spy.ndjson dela)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
