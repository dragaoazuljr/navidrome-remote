"""bridge.js injection into HTML coming from the upstream.

Strategy (spec v1.2, componente C — "sem fork"):
  * insert `<script src="/bridge.js"></script>` synchronously, immediately
    before `</head>` (preferred) so it executes before the player bundle in
    the body;
  * fallback anchors: after the `<body ...>` tag, then after the `<html ...>`
    tag, in case a document variant lacks a head;
  * idempotent: if the document already references `/bridge.js` (e.g. a
    request traversing two proxies of ours) nothing is inserted.

Case-insensitive byte scanning: HTML tags are ASCII; the body bytes are
untouched otherwise, so the transform is lossless for UTF-8 documents.
"""

from __future__ import annotations

BRIDGE_REF = "/bridge.js"
SCRIPT_TAG = f'<script src="{BRIDGE_REF}"></script>'


def inject_bridge(html: bytes) -> "tuple[bytes, bool]":
    """Return (new_html, transformed). transformed=False means the document
    was left untouched (already present, no usable anchor, or not HTML)."""
    if BRIDGE_REF.encode() in html:
        return html, False

    low = html.lower()
    anchor = low.find(b"</head>")
    if anchor >= 0:
        ins = anchor
        out = html[:ins] + _encoded_tag() + html[ins:]
        return out, True

    for open_tag in (b"<body", b"<html"):
        pos = low.find(open_tag)
        if pos < 0:
            continue
        end = low.find(b">", pos)
        if end < 0:
            continue
        ins = end + 1
        out = html[:ins] + b"\n" + _encoded_tag() + html[ins:]
        return out, True

    return html, False


def _encoded_tag() -> bytes:
    return SCRIPT_TAG.encode()
