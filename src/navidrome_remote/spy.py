"""NDJSON spy — the M0' evidence recorder (espiao).

One JSON object per line in `NVRA_SPY_FILE`, append-only, fsync'd per event
(LAN control-plane volumes make fsync affordable; a crash loses at most the
in-flight line). This file is the raw material for:

  * M0' acceptance item (5): "espião registrando o boot do player" — a
    player boot is the event sequence correlated by `cid` (client
    address:port);
  * cláusula B reds: any `upgrade_fail`/`err` line with an interrupted
    handshake is the RED evidence (B(i));
  * cláusula A: the OPTIONS/CORS map is reconstructed from `req`/`res`
    pairs;
  * M1: the session-gate decision consumes `set_cookie`/`cookie` name
    sequences.

Credential invariant (writer level — A2.3): episode A2.2 leaked raw JWTs
through this very recorder because the guarantee lived only on the
caller side. The spy is the LAST hop before the audit file, so it now
redacts at the emission chokepoint, regardless of what a caller passes:

  * fields with a sensitive name (token, jwt, password, set_cookie,
    auth-like ...) -> "[REDACTED]" (containers included);
  * header-shaped dicts ("headers", "req_headers", ...) -> per-key:
    Authorization/Cookie/& co. values redacted, benign headers (names
    AND values) survive for the clause-A reconstruction;
  * ANY string value that LOOKS like a secret (JWT shape, long hex
    digest, padded base64, "Bearer x", "user:hexcred") -> "[REDACTED]"
    even under a benign name;
  * query strings of `path`/`target`: sensitive PARAM NAMES and
    secret-shaped values redacted; benign params survive (A3).

Values that look like secrets are redacted EVEN UNDER BENIGN NAMES.
The bias is deliberate over-redaction: this file is audit evidence,
not a replay log. Names, routes, statuses and timings always travel.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode

# ---------------------------------------------------------------- M1/A3
# CREDENTIAL INVARIANT (spy side): what the spy persists is part of the
# audit trail, so it obeys the same rule as every response: no token,
# salt, user, or credential value ever hits this file. Parameter NAMES
# travel; their values are REDACTED for the sensitive set. Values that
# LOOK like secrets (JWT-shaped, long hex digests, long base64) are
# redacted even under benign parameter names.

# A2.3: "p" joins the A3 set — the Subsonic plain-password vector was
# the one credential parameter whose WEAK values slipped the shape
# detectors (only long/hex secrets were caught). A password value is
# a credential by definition; its name says so.
_REDACTED_NAMES = frozenset({
    "t", "s", "u", "p", "token", "jwt", "sig", "signature", "key",
    "apikey", "api_key", "password", "passwd", "pwd", "secret", "session",
    "auth", "auth_token", "nd_session", "nonce", "csrf", "ticket",
})
_HEX_CHARS = frozenset("0123456789abcdef")
_B64_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/")

# ---------------------------------------------------------------- A2.3
# Sensitivity sets for the writer-level chokepoint.

# Header field-names (dict keys) whose VALUE is a credential: redacted.
_HEADER_SENSITIVE = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "www-authenticate", "x-api-key", "api-key", "apikey",
    "x-xsrf-token", "x-csrftoken", "x-csrf-token",
    "access-token", "session", "session-token",
})

# Top-level field names that carry a credential payload outright:
# the WHOLE value (container or string) becomes "[REDACTED]". Includes
# the Subsonic parameter names (u/p/t/s) as FIELD names: the house
# rule is that no user/token/salt VALUE ever touches the audit file,
# whether it traveled in the query or as a bare field.
_FIELD_SENSITIVE = _HEADER_SENSITIVE | frozenset({
    "token", "jwt", "authorization", "auth", "password", "passwd",
    "pwd", "secret", "api_key", "apikey", "session", "session_value",
    "cookie", "set_cookie", "cookies", "set_cookies",
    "credential", "credentials", "private_key",
    "u", "p", "t", "s", "salt",
})

# Field names that ARE header maps: walked PER KEY instead of nuked,
# so benign headers (Accept, Origin, User-Agent, ...) survive the
# clause-A reconstruction while credentialed ones vanish.
_HEADERS_MAP_FIELDS = frozenset({
    "headers", "http_headers", "req_headers", "res_headers",
    "request_headers", "response_headers", "auth_headers",
})

# Value-shape detectors (applied to ANY string, even benign names).
_SECRET_STR_RX = (
    re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{2,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b[\w.-]{1,32}:[0-9a-fA-F]{32,}\b"),
)


def _looks_like_secret(value: str) -> bool:
    v = value.strip()
    if not v:
        return False
    if v.startswith("ey") and v.count(".") >= 2:
        return True  # JWT shape
    lv = v.lower()
    if len(lv) >= 32 and all(c in _HEX_CHARS for c in lv):
        return True  # long hex digest (Subsonic t=, HMACs)
    if len(v) >= 24 and all(c in _B64_CHARS for c in v[:-2]) and v[-2:] == "==":
        return True  # long base64 blob
    return False


def redact_str(value: str) -> str:
    """Redact a string if it looks like (or contains) a secret."""
    if any(rx.search(value) for rx in _SECRET_STR_RX):
        return "[REDACTED]"
    if _looks_like_secret(value):
        return "[REDACTED]"
    return value


def redact_query(target: str) -> str:
    """Strip/redact secret-bearing query pairs from a request target.

    The path (no query) is preserved exactly; sensitive parameter VALUES
    become "[REDACTED]" so the audit trail stays usable (names, routes,
    timestamps) while the credential invariant holds.
    """
    if "?" not in target:
        return target
    path, _, qs = target.partition("?")
    keep = []
    for k, v in parse_qsl(qs, keep_blank_values=True):
        if k.lower() in _REDACTED_NAMES or _looks_like_secret(v):
            keep.append((k, "[REDACTED]"))
        else:
            keep.append((k, v))
    return path + "?" + urlencode(keep)


def _walk(value, sensitive_key: bool):
    """Recursive redaction of one field value.

    ``sensitive_key`` = the KEY carrying this value is in the sensitive
    set -> the whole value is nuked (no salvage). Nested containers are
    walked with per-key sensitivity, so a benign container smuggles
    nothing while keeping its non-sensitive members.
    """
    if sensitive_key:
        return "[REDACTED]"
    if isinstance(value, str):
        # Strings that look like request URIs carry the same query
        # secrets as `path` (episode A2.2 pattern); scrub them too,
        # however deeply nested.
        if ("?" in value) and (
            value.startswith("/") or "://" in value
        ):
            value = redact_query(value)
        return redact_str(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            lk = str(k).lower()
            out[k] = _walk(v, lk in _FIELD_SENSITIVE or lk in _REDACTED_NAMES)
        return out
    if isinstance(value, (list, tuple)):
        return [_walk(v, sensitive_key) for v in value]
    return value


def _redact_fields(fields: dict) -> None:
    """Writer-level chokepoint: applied to EVERY event, in place."""
    for name in ("path", "target"):
        val = fields.get(name)
        if isinstance(val, str) and "?" in val:
            fields[name] = redact_query(val)
    for name in list(fields.keys()):
        lname = name.lower()
        if lname in _HEADERS_MAP_FIELDS and isinstance(fields[name], dict):
            fields[name] = _walk(
                {k: v for k, v in fields[name].items()}, False)
            # _walk with sensitive_key=False walks the dict PER KEY:
            # credentialed headers redact, benign ones survive.
            continue
        fields[name] = _walk(fields[name], lname in _FIELD_SENSITIVE)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.monotonic()*1000)%1000:03d}" + "Z"


def ts_iso(ms: "int | float | None" = None) -> str:
    """Wall-clock ISO-8601 (UTC) with millisecond precision."""
    if ms is None:
        s = time.time()
    else:
        s = ms / 1000.0 if ms > 100_000 else ms
    sec, frac = divmod(float(s) * 1000.0, 1000.0)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec)) + f".{frac:03.0f}Z"


class Spy:
    def __init__(self, path: str):
        self.path = path
        self.enabled = True
        self._lock = asyncio.Lock()
        try:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            # touch so a missing file is never a runtime surprise
            if not os.path.exists(path):
                with open(path, "a", encoding="utf-8"):
                    pass
        except OSError as exc:
            # Spy is ancillary: a broken path must never take down the
            # proxy. Log loudly, disable, continue.
            print(f"[spy] DESABILITADO (falha abrindo {path}): {exc}", file=sys.stderr)
            self.enabled = False

    async def event(self, kind: str, **fields) -> None:
        if not self.enabled:
            return
        # M1/A2.3 redaction: never persist a secret value into the audit
        # trail, no matter which caller emitted the event.
        _redact_fields(fields)
        rec = {"ts": ts_iso(), "event": kind}
        rec.update(fields)
        try:
            line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            print(f"[spy] evento inserializável ({kind}): {exc}", file=sys.stderr)
            return
        async with self._lock:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    os.write(fd, (line + "\n").encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                print(f"[spy] falha gravando {self.path}: {exc}", file=sys.stderr)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def cookie_names(cookie_header: "str | None") -> "list[str]":
        """Names only — values are credentials (invariante)."""
        if not cookie_header:
            return []
        out = []
        for part in cookie_header.split(";"):
            part = part.strip()
            if "=" in part:
                name = part.split("=", 1)[0].strip()
                if name and name not in out:
                    out.append(name)
        return out
