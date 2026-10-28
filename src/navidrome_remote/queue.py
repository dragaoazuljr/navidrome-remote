"""Persistent queue/state store (spec v1.2, M1 — card A3).

The `queue` route of /api/v1/* implies a playlist the service REMEMBERS
across mpv restarts and service reboots (definition-of-ready item 5:
"a reprodução é independente da sessão do navegador" + reboot recovery).
This module is that memory.

Invariant compliance by construction
------------------------------------
The on-disk file and every in-memory view that may travel to a client
contain `id` and `title` ONLY. Stream URLs are DERIVED at apply-time
from `id` + the service account (they live in RAM for the lifetime of
one apply, then are dropped with the request context). No URL, token,
salt or credential is EVER serialized to disk or to a response.

Apply semantics (engine is the source of truth for PLAYBACK; this
store is the source of truth for QUEUE INTENT)
----------------------------------------------
* `apply_to_engine()` rebuilds the stream URLs from ids and drives the
  engine: loadfile(first, "replace") then pl_add for the rest, plus the
  mirrored settings (volume / shuffle / repeat loops).
* It is called by a lazy background task (best effort): it retries
  while the engine is down and stops once acknowledged (the engine's
  ack is the receipt). A failure NEVER propagates into a client
  request — the worst case is a late or missed restore, logged in the
  spy (`store_apply_*`).
"""

from __future__ import annotations

import json
import os
import tempfile
import time

from .spy import Spy

STORE_VERSION = 1
MAX_ITEMS = 64
MAX_TITLE_LEN = 160


class StoreError(ValueError):
    """Operator-facing validation failure (mapped to 400 by the API)."""


class QueueItem:
    __slots__ = ("id", "title")

    def __init__(self, id: str, title: "str | None" = None):
        self.id = str(id)
        self.title = (str(title)[:MAX_TITLE_LEN] if title else None)

    def to_safe_dict(self) -> dict:
        out: dict = {"id": self.id}
        if self.title:
            out["title"] = self.title
        return out  # no url. no token. nothing a client should not hold.


class QueueStore:
    def __init__(self, cfg, spy: Spy):
        self.cfg = cfg
        self.spy = spy
        self.path = getattr(cfg, "state_file", "/var/lib/navidrome-remote/state.json")
        self.items: "list[QueueItem]" = []
        self.index: "int | None" = 0
        self.settings: dict = {"volume": None, "shuffle": 0, "repeat": "off"}
        self.pending_restore = False
        self._loaded = self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> bool:
        if not os.path.exists(self.path):
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            if obj.get("version") != STORE_VERSION:
                return False
            items = []
            for raw in obj.get("items", [])[:MAX_ITEMS]:
                iid = raw.get("id")
                if isinstance(iid, str) and iid:
                    items.append(QueueItem(iid, raw.get("title")))
            self.items = items
            idx = obj.get("index")
            self.index = int(idx) if isinstance(idx, int) and 0 <= idx < max(1, len(items)) else (0 if items else None)
            st = obj.get("settings", {})
            if isinstance(st.get("volume"), (int, float)):
                self.settings["volume"] = st["volume"]
            if isinstance(st.get("shuffle"), (int, bool)):
                self.settings["shuffle"] = int(bool(st["shuffle"]))
            rep = st.get("repeat")
            if rep in ("off", "track", "playlist"):
                self.settings["repeat"] = rep
            self.pending_restore = bool(items)
            return True
        except (OSError, ValueError) as exc:
            print("[store] estado anterior ilegível (%s) — inicia vazio" % type(exc).__name__, flush=True)
            return False

    def save(self) -> None:
        """Atomic: tmp file + fsync + rename. Sanitized by construction."""
        obj = {
            "version": STORE_VERSION,
            "items": [it.to_safe_dict() for it in self.items],
            "index": self.index,
            "settings": dict(self.settings),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
            dirname = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(dirname, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".nvr-state-", dir=dirname)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)
            os.rename(tmp, self.path)
        except OSError as exc:
            # durability is best-effort: a broken store path must never
            # take down the control plane (same philosophy as the spy).
            print("[store] falha gravando %s: %s" % (self.path, exc), flush=True)

    # ------------------------------------------------------------- mutations
    def replace(self, items: "list[dict]") -> None:
        self.items = [_coerce(it) for it in items]
        if not self.items:
            raise StoreError("fila vazia (mínimo 1 item)")
        self.index = 0
        self.pending_restore = False
        self.save()

    def append(self, items: "list[dict]") -> None:
        if len(self.items) + len(items) > MAX_ITEMS:
            raise StoreError("fila excede %d itens" % MAX_ITEMS)
        self.items.extend(_coerce(it) for it in items)
        if self.index is None and self.items:
            self.index = 0
        self.pending_restore = False
        self.save()

    def mark_selected(self, idx: "int | None") -> None:
        if idx is not None and not (0 <= idx < len(self.items)):
            idx = max(0, len(self.items) - 1) if self.items else None
        self.index = idx

    def step(self, delta: int) -> "int | None":
        if not self.items or self.index is None:
            return self.index
        n = len(self.items)
        rep = self.settings.get("repeat")
        if delta > 0:
            nxt = self.index + 1
            if nxt >= n:
                nxt = 0 if rep == "playlist" else n - 1
        else:
            nxt = self.index - 1
            if nxt < 0:
                nxt = 0
        self.index = nxt
        return nxt

    def touch_settings(self, volume=None, shuffle=None, repeat=None) -> None:
        if volume is not None:
            self.settings["volume"] = volume
        if shuffle is not None:
            self.settings["shuffle"] = int(bool(shuffle))
        if repeat is not None:
            self.settings["repeat"] = repeat
        self.save()

    def note_replacement(self, played: "dict") -> None:
        """A direct play (url/id outside the queue) resets the queue to
        that single item, keeping views consistent."""
        iid = played.get("id")
        if isinstance(iid, str) and iid:
            self.items = [QueueItem(iid, played.get("title"))]
            self.index = 0
            self.pending_restore = False
            self.save()
            return
        # opaque url play: the engine leaves the queue, but the QUEUE
        # INTENT survives (suspended: index=None) — wiping it would
        # destroy a playlist the user built.
        if self.items:
            self.index = None
            self.save()

    # ---------------------------------------------------------------- views
    def safe_snapshot(self) -> dict:
        """Sanitized: what a client may lawfully receive."""
        return {
            "length": len(self.items),
            "index": self.index,
            "items": [it.to_safe_dict() for it in self.items],
        }

    def settings_snapshot(self) -> dict:
        return dict(self.settings)


def _coerce(item: "dict") -> QueueItem:
    if not isinstance(item, dict):
        raise StoreError("item inválido (objeto esperado)")
    iid = item.get("id")
    if not isinstance(iid, str) or not iid.strip():
        raise StoreError("item sem 'id' válido")
    title = item.get("title")
    if title is not None and not isinstance(title, str):
        title = str(title)
    qi = QueueItem(iid.strip(), title)
    if "url" in item and item["url"] not in (None, ""):
        # refuse at the boundary: a raw URL in the queue is the exact
        # vector the invariant bans (client-directed fetch in mpv).
        raise StoreError("'url' não é aceito na fila (invariante de credenciais)")
    return qi


async def apply_to_engine(store: QueueStore, engine, spy: Spy) -> bool:
    """Rebuild stream URLs from ids and drive the engine.

    Best effort by contract: returns True iff the engine acked the first
    loadfile. Failures log `store_apply_failed` and leave `pending_restore`
    set (the lazy task retries).
    """
    from .api import subsonic_stream_url

    if not store.items:
        store.pending_restore = False
        return True
    if not (store.cfg.nav_user and store.cfg.nav_pass):
        await spy.event("store_apply_skipped", reason="sem-credenciais-navidrome",
                        items=len(store.items))
        store.pending_restore = False
        return False
    try:
        urls = [
            subsonic_stream_url(store.cfg.upstream_base_url, it.id,
                                store.cfg.nav_user, store.cfg.nav_pass)
            for it in store.items
        ]
    except Exception as exc:
        await spy.event("store_apply_failed", reason="url-build", error=str(exc))
        return False
    try:
        engine.cmd(["loadfile", urls[0], "replace"])
        for u in urls[1:]:
            engine.cmd(["playlist-add", u])
        st = store.settings
        if st.get("volume") is not None:
            engine.cmd(["set_property", "volume", float(st["volume"])])
        engine.cmd(["set_property", "shuffle", int(bool(st.get("shuffle")))])
        lp = 0
        lf = 0
        rep = st.get("repeat") or "off"
        if rep == "track":
            lf = -1
        elif rep == "playlist":
            lp = -1
        engine.cmd(["set_property", "loop-playlist", lp])
        engine.cmd(["set_property", "loop-file", lf])
    except Exception as exc:
        await spy.event("store_apply_failed", reason="ipc", error=str(exc))
        return False
    store.pending_restore = False
    await spy.event("store_apply_ok", items=len(urls))
    return True
