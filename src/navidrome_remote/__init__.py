"""navidrome-remote — serviço único do SBC (Orange Pi Zero 2W).

Transparent reverse proxy of the official Navidrome UI + injected bridge.js
+ minimal /api/v1/* control API driving an mpv-class playback engine over
JSON-IPC. See README for architecture; spec v1.2 in the project KB.

Design anchors:
  * stdlib-only (asyncio): zero third-party / compiled dependencies, so the
    identical tree runs on the x86 dev-workspace (C1) and the arm64 2W
    without any rebuild (agnosticidade — spec v1.2, B2).
  * byte-transparency: the HTTP layer is only terminated for our own routes
    (/bridge.js, /api/v1/*) and for HTML transformation (bridge.js
    injection). Everything else — including WebSocket upgrades and cookie
    traffic — is forwarded verbatim (cláusula B, greens B(i)/B(ii)).
  * credential invariant: stream tokens / Navidrome credentials never reach
    the browser; they live only in the server EnvironmentFile.
"""

SERVICE_NAME = "navidrome-remote"
SERVICE_VERSION = "0.1.0-m0prime"
NAVIDROME_ANCHOR = "v0.64.2"
SUBSONIC_CLIENT_VER = "1.12.1"

__all__ = [
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "NAVIDROME_ANCHOR",
    "SUBSONIC_CLIENT_VER",
]
