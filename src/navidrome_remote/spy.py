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

Credential invariant: cookie VALUES are never recorded — names only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time


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
