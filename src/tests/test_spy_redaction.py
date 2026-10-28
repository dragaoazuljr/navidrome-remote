"""A2.3 — spy writer-level credential invariant (self-test).

Episode A2.2 (raw JWTs persisted by the telemetry recorder through a
plain `path` query) proved the caller-side promise was not a control:
the redaction must live at the emission chokepoint, applied to EVERY
event, whatever the caller passes. This suite locks that in:

  * `redact_query` (A3 behavior): sensitive param names and
    secret-shaped values -> "[REDACTED]"; benign params survive;
  * `redact_str`: JWT shape, long hex, padded base64, "Bearer x",
    "user:hexcred" (also embedded in longer strings) -> "[REDACTED]";
    ordinary values untouched;
  * `_redact_fields`: sensitive field names nuke the whole value;
    header-shaped maps are walked per key (Authorization/Cookie redact,
    Accept/Origin/User-Agent survive); nested containers are walked;
    benign scalar/string fields untouched;
  * end-to-end on the real `Spy`: a hostile event (JWT in the query,
    Bearer in Authorization, cookie jar, long salt) lands on the NDJSON
    file with ZERO secret material, "[REDACTED]" markers in place, and
    the structural fields (ts/event/path/code) intact.

Every secret below is synthetic (test data only — see the episode
report for why the real ones never belong in a tree).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qsl

from navidrome_remote.spy import (
    Spy,
    _redact_fields,
    redact_query,
    redact_str,
)

# ----------------------------------------------------------- synthetics
SYN_JWT = (
    "eyJhbGciOiJIUzI1NiJ9."
    "eyJzdWIiOiJzeW50aCIsImVwIjoxLCJpc3MiOiJUUCJ9."
    "SyNh23ZzQr8mVq7xAboL5Wf"
)
SYN_SALT = "9f2ceba91b6d4a3c88e07d55f1a2b7c4"          # long hex (t=/salt)
SYN_CREDC = "sbc:" + "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678abcd"
SYN_B64 = "A" * 22 + "=="                               # padded base64 blob
SYN_HDR_AUTH = "***" + " " + SYN_JWT

SAFE_PATH = "/rest/getIndexes?letter=A&c=NavidromeUI"
SAFE_NOTE = "ordinary value"


class RedactUnitTest(unittest.TestCase):
    # ------------------------------------------------------------ query
    def test_query_sensitive_names_redacted(self):
        out = redact_query("/x?a=1&u=someuser&p=mypassword&t=" + SYN_SALT)
        self.assertTrue(out.startswith("/x?"))
        # NOTE: the query is urlencoded (house format, A3) so the marker
        # travels percent-encoded; compare via the DECODED value.
        q = dict(parse_qsl(out.split("?", 1)[1]))
        self.assertEqual(q["a"], "1")
        self.assertEqual(q["u"], "[REDACTED]")
        self.assertEqual(q["p"], "[REDACTED]")
        self.assertEqual(q["t"], "[REDACTED]")

    def test_query_secret_shape_under_benign_name(self):
        out = redact_query("/api/events?meta=" + SYN_JWT)
        self.assertNotIn(SYN_JWT, out)
        self.assertEqual(dict(parse_qsl(out.split("?", 1)[1]))["meta"],
                         "[REDACTED]")

    def test_query_clean_passthrough(self):
        self.assertEqual(redact_query(SAFE_PATH), SAFE_PATH)
        self.assertEqual(redact_query("/noquery"), "/noquery")

    # -------------------------------------------------------------- str
    def test_str_jwt_exact_and_embedded(self):
        self.assertEqual(redact_str(SYN_JWT), "[REDACTED]")
        self.assertEqual(redact_str("got " + SYN_JWT + " here"), "[REDACTED]")

    def test_str_credential_pair_and_long_hex_and_b64(self):
        self.assertEqual(redact_str(SYN_CREDC), "[REDACTED]")
        self.assertEqual(redact_str(SYN_SALT), "[REDACTED]")
        self.assertEqual(redact_str(SYN_B64), "[REDACTED]")

    def test_str_bearer_prefix(self):
        self.assertEqual(redact_str(SYN_HDR_AUTH), "[REDACTED]")

    def test_str_benign_untouched(self):
        self.assertEqual(redact_str(SAFE_NOTE), SAFE_NOTE)
        self.assertEqual(redact_str("200"), "200")
        self.assertEqual(redact_str("/app"), "/app")
        self.assertEqual(redact_str(""), "")

    # ---------------------------------------------------------- fields
    def _fields(self):
        return {
            "path": "/api/v1/play?jwt=" + SYN_JWT,
            "code": 201,
            "cid": "192.168.0.7:52344",
            "headers": {
                "Authorization": SYN_HDR_AUTH,
                "Cookie": "nd_session=q9x7vB2L; theme=dark",
                "Accept": "application/json",
                "User-Agent": "NavidromeUI/0.64.2",
            },
            "set_cookie": "nd_session=" + SYN_B64 + "; Path=/; HttpOnly",
            "note": SAFE_NOTE,
            "queue": [{"token": SYN_JWT}, {"id": "song-1"}],
        }

    def test_fields_redaction_matrix(self):
        f = self._fields()
        _redact_fields(f)
        # query: name 'jwt' redacted (percent-encoded marker, house
        # format), path preserved
        self.assertTrue(f["path"].startswith("/api/v1/play?"))
        self.assertNotIn(SYN_JWT, f["path"])
        self.assertEqual(
            dict(parse_qsl(f["path"].split("?", 1)[1]))["jwt"], "[REDACTED]")
        # struct passthrough
        self.assertEqual(f["code"], 201)
        self.assertEqual(f["note"], SAFE_NOTE)
        # headers map: per-key
        self.assertEqual(f["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(f["headers"]["Cookie"], "[REDACTED]")
        self.assertEqual(f["headers"]["Accept"], "application/json")
        self.assertEqual(f["headers"]["User-Agent"], "NavidromeUI/0.64.2")
        # sensitive top-level field nukes the value outright
        self.assertEqual(f["set_cookie"], "[REDACTED]")
        self.assertNotIn(SYN_B64, json.dumps(f))
        # nested container: sensitive keys redacted, benign kept
        self.assertEqual(f["queue"][0]["token"], "[REDACTED]")
        self.assertEqual(f["queue"][1]["id"], "song-1")

    def test_fields_already_redacted_marker_is_stable(self):
        f = {"path": "a?jwt=[REDACTED]"}
        _redact_fields(f)
        # marker stays a marker (no doubling); in `path` form the
        # query is urlencoded per house format
        q = dict(parse_qsl(f["path"].split("?", 1)[1]))
        self.assertEqual(q["jwt"], "[REDACTED]")
        self.assertNotIn("[REDACTED]%3D[REDACTED]", f["path"])


class SpyEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = os.path.join(self.tmp.name, "spy.ndjson")
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)
        self.tmp.cleanup()

    def _emit(self, kind, **fields):
        spy = Spy(self.file)
        self.loop.run_until_complete(spy.event(kind, **fields))

    def test_hostile_event_never_leaks(self):
        self._emit(
            "req",
            path="/rest/getIndexes?letter=A&jwt=" + SYN_JWT,
            headers={
                "Authorization": SYN_HDR_AUTH,
                "Cookie": "nd_session=" + SYN_SALT + "; c=2",
                "Accept": "text/html",
            },
            u="sbc",
            p=SYN_CREDC,
            code=200,
        )
        with open(self.file, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        raw = lines[0]
        rec = json.loads(raw)
        # structural fields survived
        self.assertEqual(rec["event"], "req")
        self.assertIn("ts", rec)
        self.assertEqual(rec["code"], 200)
        self.assertNotIn(SYN_JWT, rec["path"])
        self.assertEqual(
            dict(parse_qsl(rec["path"].split("?", 1)[1]))["jwt"], "[REDACTED]")
        self.assertIn("letter=A", rec["path"])
        # the whole file must be clean of every synthetic secret
        for syn in (SYN_JWT, SYN_SALT, SYN_CREDC, SYN_B64, SYN_HDR_AUTH):
            self.assertNotIn(syn, raw, "secret material leaked into NDJSON")
        # ... and the sensitive carriers carried markers, not values
        self.assertEqual(rec["p"], "[REDACTED]")
        self.assertEqual(rec["headers"]["Cookie"], "[REDACTED]")
        self.assertEqual(rec["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(rec["headers"]["Accept"], "text/html")


if __name__ == "__main__":
    unittest.main(verbosity=2)
