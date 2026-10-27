"""Unit tests for the http1 codecs (fast, no sockets)."""

from __future__ import annotations

import unittest
import zlib

from navidrome_remote.http1 import (
    HeadList,
    WireError,
    chunk_encode,
    decode_body,
    detect_ce,
    gzip_compress,
    int_or_zero,
)
from navidrome_remote.inject import inject_bridge


class CodecTests(unittest.TestCase):
    def test_chunk_roundtrip_single(self):
        data = b"0123456789abcdef"
        enc = chunk_encode(data)
        self.assertTrue(enc.startswith(b"10\r\n"))
        self.assertTrue(enc.endswith(b"0\r\n\r\n"))

    def test_chunk_roundtrip_multiple_frames(self):
        data = b"x" * 70000  # forces >1 frame at 65536
        enc = chunk_encode(data)
        # naive decoder
        buf = enc
        out = b""
        while True:
            nl = buf.index(b"\r\n")
            size = int(buf[:nl], 16)
            if size == 0:
                break
            out += buf[nl + 2 : nl + 2 + size]
            buf = buf[nl + 2 + size + 2 :]
        self.assertEqual(out, data)

    def test_int_or_zero(self):
        self.assertEqual(int_or_zero("12"), 12)
        self.assertEqual(int_or_zero("-5"), 0)
        self.assertEqual(int_or_zero("junk"), 0)
        self.assertEqual(int_or_zero(None), 0)

    def test_detect_ce_variants(self):
        self.assertEqual(detect_ce("gzip"), "gzip")
        self.assertEqual(detect_ce("GZIP"), "gzip")
        self.assertIsNone(detect_ce("identity"))
        self.assertIsNone(detect_ce(None))
        self.assertEqual(detect_ce("br"), "br")

    def test_gzip_decode_repair(self):
        plain = b"hello gzip"
        packed = gzip_compress(plain)
        out, used = decode_body(packed, "gzip")
        self.assertEqual(out, plain)
        self.assertEqual(used, "gzip")

    def test_deflate_zlib_wrapped(self):
        plain = b"deflate-payload"
        packed = zlib.compress(plain)
        out, used = decode_body(packed, "deflate")
        self.assertEqual(out, plain)
        self.assertEqual(used, "deflate")

    def test_headlist_duplicates_and_case(self):
        hl = HeadList()
        hl.add("Set-Cookie", "a=1")
        hl.add("set-cookie", "b=2")
        hl.add("Accept-Language", "pt")
        self.assertEqual(len(hl.get("set-cookie")), 2)
        hl.replace("accept-language", "en,pt;q=0.8")
        self.assertEqual(hl.get_first("Accept-Language"), "en,pt;q=0.8")
        hl.remove("SET-COOKIE")
        self.assertFalse(hl.has("set-cookie"))

    def test_wire_error_carries_status(self):
        e = WireError("too big", 431)
        self.assertEqual(e.status, 431)
        self.assertEqual(e.body, "too big")


class InjectTests(unittest.TestCase):
    def test_insert_before_head_close(self):
        html = b"<html><head><title>x</title></head><body>y</body></html>"
        out, ok = inject_bridge(html)
        self.assertTrue(ok)
        self.assertIn(b'<script src="/bridge.js"></script>', out)
        self.assertLess(
            out.index(b'<script src="/bridge.js">'),
            out.index(b"</head>"),
        )

    def test_case_insensitive_anchor(self):
        html = b"<html><HEAD><TITLE>x</TITLE></HEAD><body>y</body></html>".upper().replace(b"<HTML>", b"<html>").replace(b"<BODY>", b"<body>")
        html = b"<html><Head><Title>x</Title></Head><body>y</body></html>"
        out, ok = inject_bridge(html)
        self.assertTrue(ok)
        self.assertIn(b"/bridge.js", out)

    def test_body_fallback_when_no_head(self):
        html = b"<!doctype html><html><body class=x>y</body></html>"
        out, ok = inject_bridge(html)
        self.assertTrue(ok)
        i = out.index(b"/bridge.js")
        self.assertGreater(i, out.index(b"<body"))

    def test_idempotent_when_already_present(self):
        html = b'<html><head><script src="/bridge.js"></script></head><body/></html>'
        out, ok = inject_bridge(html)
        self.assertFalse(ok)
        self.assertEqual(out, html)
        self.assertEqual(html.count(b"/bridge.js"), 1)

    def test_no_anchor_leaves_untouched(self):
        html = b"just a plain document with no tags"
        out, ok = inject_bridge(html)
        self.assertFalse(ok)
        self.assertEqual(out, html)


if __name__ == "__main__":
    unittest.main()
