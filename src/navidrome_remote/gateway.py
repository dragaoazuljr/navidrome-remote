"""Session gate for /api/v1/* (spec v1.2, M1 — card A3).

Authorization for the LAN control plane is a three-layer gate. It
implements the two statements of the approved package:

  * "Sessão Navidrome ativa gateia a API" — an ACTIVE Navidrome session
    is required for every call (layers 1 + 3);
  * "token por chamada" — a session is never trusted across calls: every
    single request pays for its own verification (layer 3, per call).
    Optionally reinforced by the static NVRA_API_TOKEN (layer 0) when
    configured — see Api.handle for the exact channel matrix.

Layers
------
L0 — static token (optional, operator config): ``NVRA_API_TOKEN``.
     Kept from M0' as defence in depth for weak channels. Signed JWT
     channels carry their own credential and are exempt.
L1 — presence: the request must carry a Navidrome session in at least
     one presentation CHANNEL. v0.64.2 (anchor) speaks Subsonic
     `u`/`t`/`s` parameters on the wire — that is what the A2 boot
     capture observed from a real browser (no cookies, no Authorization
     header; the UI parks the native JWT in localStorage). Channels, in
     strength order:

       ``jwt``    — a JWT in the Authorization header, a `jwt` parameter
                    (query or POST body) or as a cookie VALUE;
       ``params`` — Subsonic `u` (+ `t`, `s` or `p`) in query string or
                    POST body (the v0.64.2 wire reality);
       ``cookie`` — an opaque Cookie header (future versions that set
                    cookies; identity UNKNOWN on this channel).

     Absence of all three → 401 ``session-missing``.
L2 — identity: the session must not belong to the SBC SERVICE account
     (``NVRA_NAV_USER``). That account's credential is a server-side
     secret (invariante de credenciais): a session presenting it from
     the browser plane is misuse, even when perfectly valid upstream
     (e.g. somebody opened the UI with the service login — exactly what
     the A2 boot capture shows: u=sbc). Detection: plaintext `u` on the
     params channel; decoded JOSE claim on the jwt channel. Opaque
     cookies cannot be tied to an account → the layer cannot fire
     (documented residual).  Match → 403 ``service-account-forbidden``
     BEFORE any probe (nothing about the session's validity leaks).
L3 — liveness: the session is verified PER CALL against the upstream —
     the authority — with one cheap authenticated probe
     (`GET /rest/getIndexes?letter=A`, measured present on v0.64.2 in
     A0.1; authenticated-only, side-effect free, small):

       success (subsonic `success`; code 50 "not authorized" ALSO
         proves the session authenticated)                 → ALLOW
       subsonic `code:40` or HTTP 401                     → 401 session-invalid
       probe route vanished (HTTP 404) — anchor breakage  → 401 session-unverifiable
                                                            (FAIL CLOSED + spy red)
       transport error / upstream 5xx                     → 401 session-unverifiable

     A short (10 s) dedupe window absorbs storms of IDENTICAL
     credentials (tight retry loops, multi-tab polls) so one logical
     session costs one probe per window. That is RATE protection, NOT
     trust extension: different credentials always re-probe, and the
    window always lapses.

Credential invariant
--------------------
The gate puts no value anywhere a client or the spy can read:
responses carry only machine codes + a detail line; spy events carry
channel and (params only) the USERNAME — identifier, not secret.
`t`/`s`/`p`/jwt/cookie values are never serialized by this module.
Contract tests assert this with sentinel scans.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlsplit

from . import SUBSONIC_CLIENT_VER
from .config import Config
from .spy import Spy

PROBE_PATH = "/rest/getIndexes"
DEDUP_TTL_S = 10.0
DEDUP_MAX_ENTRIES = 256
PROBE_TIMEOUT_S = 10.0
PROBE_MAX_BODY = 1 << 16

_BEARER_RE = re.compile(r"^bearer\s+(.+)$", re.IGNORECASE)
_JWT_SHAPE_RE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")

#: Claim names consulted, in order, to identify a JWT issuer account.
#: `sub` is the RFC-standard subject; the rest are Navidrome-observed
#: spellings (the v0.64.2 layout is pinned by the A3 live gate).
IDENTITY_CLAIMS = ("username", "user", "name", "uniqueName", "sub", "account", "iss")


class GateProbeError(Exception):
    """Probe could not run (transport) — classified as unverifiable."""


@dataclass(frozen=True)
class Candidate:
    """One extracted session presentation (strongest wins)."""

    channel: str  # "jwt" | "params" | "cookie"
    user: "str | None" = None
    params: "tuple[tuple[str, str], ...]" = ()  # (u,(t),(s),(p)) as presented
    jwt: "str | None" = None  # only for the jwt channel
    cookie: "str | None" = None  # only for the cookie channel

    def dedupe_key(self) -> str:
        h = hashlib.sha256()
        h.update(self.channel.encode())
        for name, val in self.params:
            h.update(name.encode())
            h.update(val.encode())
        if self.jwt:
            h.update(("jwt:" + self.jwt).encode())
        if self.cookie:
            h.update(("cookie:" + self.cookie).encode())
        return h.hexdigest()[:32]


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    status: int  # 401 | 403 (200 implicit when allowed)
    code: str  # session-ok|session-missing|session-invalid|session-denied-recent|
    #             session-unverifiable|service-account-forbidden
    detail: str
    channel: "str | None" = None
    user: "str | None" = None


# ------------------------------------------------------------ extraction
def _looks_like_jwt(token: "str | None") -> bool:
    if not token:
        return False
    t = token.strip()
    if t.lower().startswith(("basic ", "bearer ")):
        t = t.split(None, 1)[-1].strip()
    return _JWT_SHAPE_RE.match(t) is not None


def _jwt_payload_claims(jwt: str) -> "dict | None":
    """Decode the JOSE payload WITHOUT verifying the signature.

    Signature/expiration validity is the UPSTREAM's job (L3); we only
    need the identity claim for L2. Malformed → None (unknown identity).
    """
    parts = jwt.strip().split(".")
    if len(parts) < 2:
        return None
    pad = "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        obj = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _user_from_claims(claims: "dict | None") -> "str | None":
    if not claims:
        return None
    for name in IDENTITY_CLAIMS:
        v = claims.get(name)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _first(d: "dict[str, list[str]]", key: str) -> "str | None":
    vals = d.get(key)
    return vals[0].strip() if vals else None


def extract_candidate(headers, target: str, payload: "dict | None") -> "Candidate | None":
    """Strongest session presentation in the request (or None).

    Sources: Authorization header > query string > POST body > Cookie.
    """
    q: "dict[str, list[str]]" = {}
    if "?" in target:
        for k, v in parse_qsl(urlsplit(target).query, keep_blank_values=True):
            q.setdefault(k.lower(), []).append(v)
    b: "dict[str, list[str]]" = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(v, (str, int, float, bool)):
                b.setdefault(str(k).lower(), []).append(str(v))

    # --- channel 1 (strongest): JWT
    jwt = None
    auth = (headers.get_first("authorization") or "").strip()
    m = _BEARER_RE.match(auth)
    if m and _looks_like_jwt(m.group(1)):
        jwt = m.group(1).strip()
    if jwt is None:
        for src in (q, b):
            jv = _first(src, "jwt")
            if jv and _looks_like_jwt(jv):
                jwt = jv
                break
    if jwt is None:
        ck = headers.get_first("cookie") or ""
        for part in ck.split(";"):
            if "=" in part:
                val = part.split("=", 1)[1].strip()
                if _looks_like_jwt(val):
                    jwt = val
                    break
    if jwt is not None:
        return Candidate(channel="jwt", user=_user_from_claims(_jwt_payload_claims(jwt)), jwt=jwt)

    # --- channel 2: Subsonic u/(t|s|p)
    user = _first(q, "u") or _first(b, "u")
    if user is not None:
        src_u = q if _first(q, "u") else b
        other = b if src_u is q else q
        params: "list[tuple[str, str]]" = [("u", user)]
        for name in ("t", "s", "p"):
            v = _first(src_u, name) or _first(other, name)
            if v:
                params.append((name, v))
        return Candidate(channel="params", user=user, params=tuple(params))

    # --- channel 3: opaque cookie
    ck = (headers.get_first("cookie") or "").strip()
    if ck:
        return Candidate(channel="cookie", cookie=ck)
    return None


# ------------------------------------------------------------- the gate
class SessionGate:
    """Construct once with the service's Config + Spy."""

    def __init__(self, cfg: Config, spy: Spy, clock=time.monotonic):
        self.cfg = cfg
        self.spy = spy
        self._clock = clock
        self._dedupe: "dict[str, tuple[float, Verdict]]" = {}
        self.probe_calls = 0  # instrumentation (tests / spy correlation)

    async def evaluate(self, headers, target: str, payload: "dict | None") -> Verdict:
        cand = extract_candidate(headers, target, payload)
        if cand is None:
            await self._log("gate_deny_missing", channel="-", user=None)
            return Verdict(False, 401, "session-missing",
                           "nenhuma sessão Navidrome apresentada", "-")

        # L2 — service account from the browser plane (before any probe).
        if self._is_service_account(cand.user):
            await self._log("gate_deny_service_account",
                            channel=cand.channel, user=cand.user)
            return Verdict(False, 403, "service-account-forbidden",
                           "a conta de serviço é restrita ao lado servidor",
                           cand.channel)

        # dedupe window: rate protection, not trust extension
        self._gc_dedupe()
        key = cand.dedupe_key()
        hit = self._dedupe.get(key)
        if hit is not None and (self._clock() - hit[0]) < DEDUP_TTL_S:
            verdict = hit[1]
            if verdict.allowed:
                return verdict
            return Verdict(False, verdict.status, "session-denied-recent",
                           "tentativa recente negada — tente novamente em instantes",
                           cand.channel)

        try:
            ok, why = await self._probe_upstream(cand)
        except GateProbeError as exc:
            ok, why = False, str(exc)

        if ok:
            verdict = Verdict(True, 200, "session-ok", "", cand.channel, cand.user)
            await self._log("gate_allow", channel=cand.channel, user=cand.user)
        else:
            status, code = self._classify_failure(why)
            verdict = Verdict(False, status, code, why, cand.channel, cand.user)
            await self._log("gate_deny_" + code.replace("-", "_"),
                            channel=cand.channel, user=cand.user,
                            reason=why[:160])
        self._dedupe[key] = (self._clock(), verdict)
        return verdict

    def _is_service_account(self, user: "str | None") -> bool:
        if not user or not self.cfg.nav_user:
            return False
        return user.casefold() == self.cfg.nav_user.casefold()

    @staticmethod
    def _classify_failure(why: str) -> "tuple[int, str]":
        if "route-absent" in why:
            return 401, "session-unverifiable"
        if why.startswith(("http-5", "transport", "probe-params")):
            return 401, "session-unverifiable"
        return 401, "session-invalid"

    def _gc_dedupe(self) -> None:
        now = self._clock()
        for k in [k for k, (t, _) in self._dedupe.items() if now - t > DEDUP_TTL_S]:
            self._dedupe.pop(k, None)
        if len(self._dedupe) > DEDUP_MAX_ENTRIES:
            for k, _ in sorted(self._dedupe.items(), key=lambda kv: kv[1][0]):
                self._dedupe.pop(k, None)
                if len(self._dedupe) <= DEDUP_MAX_ENTRIES // 2:
                    break

    # ---------------------------------------------------------------- probe
    async def _probe_upstream(self, cand: Candidate) -> "tuple[bool, str]":
        """One authenticated GET against the upstream, as the presenter would.

        Returns (ok, why). `why` is an internal classification string —
        it never contains a credential value. Any INTERNAL (structural)
        surprise degrades to an unverifiable probe: a broken probe must
        never yield "allowed", and must never tear the client connection
        (fail closed, loudly, at the caller).
        """
        self.probe_calls += 1
        try:
            return await self._do_probe(cand)
        except GateProbeError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, "probe-params: %s" % type(exc).__name__

    async def _do_probe(self, cand: Candidate) -> "tuple[bool, str]":
        """(raw probe machinery — errors bubble to _probe_upstream)"""
        probe_common = [("letter", "A"),
                        ("v", SUBSONIC_CLIENT_VER),
                        ("c", "navidrome-remote-gate")]

        if cand.channel == "jwt":
            query = "&".join("%s=%s" % (n, quote(v, safe="")) for n, v in probe_common)
            target = PROBE_PATH + "?" + query
            hdr_pairs = [("host", self.cfg.vhost_authority),
                         ("authorization", "Bearer " + cand.jwt),
                         ("accept", "*/*"),
                         ("connection", "close")]
        elif cand.channel == "params":
            query = "&".join(
                "%s=%s" % (n, quote(v, safe=""))
                for pair in probe_common for n, v in [pair]
            )
            for name, val in cand.params:
                query += "&%s=%s" % (name, quote(val, safe=""))
            target = PROBE_PATH + "?" + query
            hdr_pairs = [("host", self.cfg.vhost_authority),
                         ("accept", "*/*"),
                         ("connection", "close")]
        else:  # opaque cookie
            query = "&".join("%s=%s" % (n, quote(v, safe="")) for n, v in probe_common)
            target = PROBE_PATH + "?" + query
            hdr_pairs = [("host", self.cfg.vhost_authority),
                         ("cookie", cand.cookie or ""),
                         ("accept", "*/*"),
                         ("connection", "close")]

        wire = ("GET %s HTTP/1.1\r\n" % target).encode("latin-1")
        wire += b"".join(("%s: %s\r\n" % (n, v)).encode("latin-1") for n, v in hdr_pairs)
        wire += b"\r\n"

        try:
            rd, wr = await asyncio.wait_for(
                asyncio.open_connection(self.cfg.upstream_host, self.cfg.upstream_port),
                timeout=PROBE_TIMEOUT_S,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise GateProbeError("transport: %s" % type(exc).__name__) from exc

        try:
            wr.write(wire)
            await asyncio.wait_for(wr.drain(), PROBE_TIMEOUT_S)
            resp, leftover = await asyncio.wait_for(self._read_head(rd), PROBE_TIMEOUT_S)
            if resp is None:
                raise GateProbeError("transport: upstream fechou sem resposta")
            body = await asyncio.wait_for(
                self._read_body(rd, leftover, resp), PROBE_TIMEOUT_S
            )
        finally:
            try:
                wr.close()
            except (OSError, RuntimeError):
                pass

        if resp.status == 401:
            return False, "http-401: upstream rejeitou a sessão"
        if resp.status == 404:
            # anchor breakage: the probe route vanished on an upstream
            # upgrade. Fail closed + loud spy red (protocol red → decision, R2).
            return False, "route-absent: " + PROBE_PATH
        if resp.status >= 500:
            return False, "http-%d: upstream degradado" % resp.status
        if resp.status != 200:
            return False, "http-%d: resposta inesperada" % resp.status

        obj = _try_json(body)
        if obj is None:
            return True, ""  # 200 non-JSON: authenticated (route variant)
        code = obj.get("code")
        if code in (None, 0):
            return True, ""
        if code == 40:
            return False, "subsonic-code-40: credencial inválida"
        if code in (30, 34):
            return False, "probe-params-%s" % code
        # any other code that reaches a per-user handler (e.g. 50
        # "not authorized") proves the session authenticated.
        return True, ""

    async def _read_head(self, rd):
        from .http1 import read_head
        resp, leftover = await read_head(rd, "response")
        return resp, leftover

    async def _read_body(self, rd, leftover, resp):
        from .http1 import decode_body, detect_ce, read_response_body_raw
        body, _framing = await read_response_body_raw(
            rd, leftover, resp.headers, resp.status, PROBE_MAX_BODY
        )
        ce = detect_ce(resp.headers.get_first("content-encoding"))
        if ce is not None:
            body, _used = decode_body(body, ce)
        return body

    async def _log(self, kind: str, **fields) -> None:
        await self.spy.event(kind, **fields)


def _try_json(body: "bytes | str") -> "dict | None":
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", "replace")
        except Exception:
            return None
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None
