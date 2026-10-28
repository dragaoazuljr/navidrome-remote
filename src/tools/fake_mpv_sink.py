#!/usr/bin/env python3
"""Mock mpv IPC sink — the C1 stand-in for the real playback engine (A4 v0.3).

Speaks the same mpv JSON-IPC protocol as real mpv (newline-delimited JSON
over an AF_UNIX datagram socket) so that navidrome_remote.ipc.MpvEngine is
exercised byte-for-byte on the dev-workspace, where neither mpv nor an
audio device exists (C1 per spec v1.2: "SEM validação de áudio").

Usage:
  python3 tools/fake_mpv_sink.py --socket /dev/shm/navidrome-remote-mock-ipc

Parity surface (v0.3 — property set of the mpv builds we anchor to):
  * get_version; get_property (single name, list of names -> dict);
  * set_property: pause, volume, time-pos, shuffle (WRITE-TRIGGERED
    REORDER on 0->1: current entry kept in place, original layout
    restored on 1->0 — mirrors the real engine), loop-file, loop-playlist;
  * loadfile <uri> [replace|append] [, ...] [id] — playlist-aware (M1/A4):
      replace   -> entries=[uri], play index 0
      append    -> entries += [uri], play last
      default   -> play uri at current position (seed if empty);
  * playlist-add <uri> [index] [id] (index -1/omitted = append),
    pl_clear / playlist-clear, pl_play / playlist-play <i>,
    playlist-move, playlist-delete, playlist-shuffle;
  * seek <amt> [milliseconds|relative|absolute|percent] — clamped to
    [0, duration] (boundary behaviour of the real engine);
  * properties: idle, core-idle, playback-status, pause, volume,
    time-pos, duration, media-title, filename, playlist-size (alias),
    playlist-count, playlist-index, playlist-play-index,
    playlist-current-id, playlist-current-title, shuffle, loop-file,
    loop-playlist;
  * events: idle-state flips, file-loaded (carries the entry id),
    property-change (time-pos tick); emitted events carry t_monotonic
    so the gapless best-effort metric (spacing between consecutive
    file-loaded events) is MEASURED, not assumed.

EOF auto-advance (new in v0.3): the ticker detects time-pos reaching the
current entry's duration and walks the playlist honoring
loop-file/loop-playlist — the natural-progress path the real engine
implements. A3's mock advanced only on explicit commands; the
controller's engine->store reconciliation is therefore exercised here
against natural progress as well.

On 2W/C3 this program is simply replaced by:
  mpv --idle=yes --gapless=yes --really-quiet
      --input-ipc-server=<same socket>
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import signal
import socket
import threading
import time


VERSION = "0.3.0-fake-mpv-sink"


class Entry:
    """One playlist entry: stream url + its mpv `id` (the Navidrome song
    id the controller tags it with) + a synthesized duration."""
    __slots__ = ("url", "id", "duration")

    def __init__(self, url: str, id_=None, duration: float = 180.0):
        self.url = url
        self.id = id_
        self.duration = float(duration)

    def to_wire(self) -> dict:
        d = {"url": self.url}
        if self.id is not None:
            d["id"] = self.id
        return d


class State:
    def __init__(self, duration: float = 180.0,
                 rng: "random.Random | None" = None) -> None:
        self.idle = True
        self.pause_flag = False
        self.volume = 100.0
        self.time_pos = 0.0
        self.media_title = ""
        self.current_url = ""
        self.default_duration = float(duration)
        self.rng = rng or random.Random()
        # A4: id-tagged playlist model
        self.entries: "list[Entry]" = []
        self.orig_entries: "list[Entry] | None" = None  # pre-shuffle layout
        self.pl_idx = 0
        self.shuffle = 0
        self.loop_file = 0
        self.loop_playlist = 0
        self._plays_left: "dict[int, int]" = {}

    # ------------------------------------------------------------- helpers
    def playing(self) -> bool:
        return not self.idle and not self.pause_flag

    def playback_status(self) -> str:
        if self.idle:
            return "stopped"
        return "paused" if self.pause_flag else "playing"

    @staticmethod
    def _title_of(uri: str, id_) -> str:
        # /rest/stream?id=X has no informative basename: use the entry id
        # when present (the controller reconciles on id anyway).
        if id_:
            return str(id_)
        base = uri.split("?", 1)[0].rsplit("/", 1)[-1]
        return base or uri[:64]

    @property
    def current_entry(self) -> "Entry | None":
        if 0 <= self.pl_idx < len(self.entries):
            return self.entries[self.pl_idx]
        return None

    def reset_play_count(self) -> None:
        cur = self.current_entry
        if cur is not None:
            self._plays_left[id(cur)] = 0

    def snapshot(self) -> dict:
        cur = self.current_entry
        return {
            "idle": self.idle,
            "core-idle": self.idle,
            "playback-status": self.playback_status(),
            "pause": self.pause_flag,
            "volume": self.volume,
            "time-pos": round(self.time_pos, 3),
            "duration": round(cur.duration, 3) if cur is not None else None,
            "media-title": self.media_title,
            "playlist-size": len(self.entries),        # legacy alias (A1)
            "playlist-count": len(self.entries),
            "playlist-index": self.pl_idx if self.entries else 0,
            "playlist-play-index": self.pl_idx if self.entries else 0,
            "playlist-current-id": cur.id if cur is not None else None,
            "playlist-current-title":
                self.media_title if cur is not None else None,
            "filename": self.current_url,
            "shuffle": self.shuffle,
            "loop-file": self.loop_file,
            "loop-playlist": self.loop_playlist,
        }


KNOWN_PROPS = set(State().snapshot().keys())

# mpv property name (dash spelling) -> State field (underscore spelling),
# for writable properties that have no explicit assignment branch.
PROP_FIELDS = {"loop-file": "loop_file", "loop-playlist": "loop_playlist"}

READONLY_PROPS = frozenset({
    "playback-status", "media-title", "playlist-index", "playlist-size",
    "playlist-count", "playlist-play-index", "playlist-current-id",
    "playlist-current-title", "filename", "idle", "core-idle", "duration",
})


class Sink:
    def __init__(self, socket_path: str, tick_hz: float = 4.0,
                 duration: float = 180.0, seed=None) -> None:
        self.socket_path = socket_path
        self.tick_hz = tick_hz
        self.default_duration = float(duration)
        self.state = State(duration=duration, rng=random.Random(seed))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self._ev_lock = threading.Lock()
        self.peers: "set" = set()
        self.sock: "socket | None" = None
        self.events: "list[dict]" = []  # emitted events (test introspection)

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.socket_path)
        self.sock.settimeout(0.25)
        print(f"[fake-mpv-sink] listening on {self.socket_path} (version={VERSION})",
              flush=True)
        threading.Thread(target=self._tick_loop, daemon=True).start()
        while not self.stop.is_set():
            try:
                raw, _peer = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            self._dispatch(raw, _peer)

    def shutdown(self) -> None:
        self.stop.set()
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
            self.sock = None
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    # ------------------------------------------------------------------ ticks
    def _tick_loop(self) -> None:
        last = time.monotonic()
        while not self.stop.is_set():
            now = time.monotonic()
            dt = now - last
            last = now
            st = self.state
            if st.playing():
                st.time_pos += dt
                if int(st.time_pos * self.tick_hz) > \
                        int((st.time_pos - dt) * self.tick_hz):
                    self._emit(
                        {"event": "property-change",
                         "property": "time-pos",
                         "data": round(st.time_pos, 3),
                         "t_monotonic": time.monotonic()})
                # EOF reached: the natural-advance path (the real engine
                # does this in its input thread; emulated here).
                cur = st.current_entry
                if cur is not None and cur.duration > 0 and \
                        st.time_pos >= cur.duration:
                    with self.lock:
                        self._eof_advance()
            time.sleep(0.05)

    def _emit(self, obj: dict) -> None:
        with self._ev_lock:
            self.events.append(dict(obj))
        if self.sock is None or not self.peers:
            return
        data = json.dumps(obj, separators=(",", ":"),
                          ensure_ascii=False).encode()
        for peer in list(self.peers):
            try:
                self.sock.sendto(data, peer)
            except OSError:
                self.peers.discard(peer)

    # -------------------------------------------------------------- playlist
    def _load_entry(self, entry: Entry) -> None:
        st = self.state
        st.idle = False
        st.current_url = entry.url
        st.time_pos = 0.0
        st.media_title = State._title_of(entry.url, entry.id)
        st.reset_play_count()
        self._emit({"event": "file-loaded", "filename": entry.url,
                    "media-title": st.media_title, "id": entry.id,
                    "t_monotonic": time.monotonic()})

    def _goto(self, idx: int) -> None:
        st = self.state
        if 0 <= idx < len(st.entries):
            st.pl_idx = idx
            self._load_entry(st.entries[idx])

    def _eof_advance(self) -> None:
        """Natural end-of-file transition (lock held by the caller)."""
        st = self.state
        cur = st.current_entry
        if cur is None:
            return
        # loop-file: -1 = forever (roll the clock), N >= 1 = N+1 plays,
        # 0 = once (straight to advance).
        if st.loop_file < 0:
            st.time_pos -= cur.duration
            return
        if st.loop_file >= 1:
            plays = st._plays_left.get(id(cur), 0) + 1
            st._plays_left[id(cur)] = plays
            if plays <= st.loop_file + 1:
                st.time_pos = 0.0
                self._emit({"event": "file-loaded", "filename": cur.url,
                            "media-title": st.media_title, "id": cur.id,
                            "loop": plays,
                            "t_monotonic": time.monotonic()})
                return
        # walk the (possibly shuffled) playlist
        n = len(st.entries)
        nxt = st.pl_idx + 1
        if nxt >= n:
            if st.loop_playlist < 0:
                nxt = 0
            else:
                st.idle = True
                st.pause_flag = False
                st.time_pos = 0.0
                self._emit({"event": "idle", "idle": True,
                            "t_monotonic": time.monotonic()})
                return
        self._goto(nxt)

    def _shuffle_physical(self) -> None:
        """Physical permutation keeping the CURRENT entry in place; the
        original layout is remembered so disable restores it."""
        st = self.state
        if len(st.entries) < 2:
            return
        if st.orig_entries is None:
            st.orig_entries = list(st.entries)
        current = st.pl_idx
        rest = [e for i, e in enumerate(st.entries) if i != current]
        st.rng.shuffle(rest)
        new = []
        pos = 0
        for i in range(len(st.orig_entries)):
            new.append(st.orig_entries[current] if i == current
                       else rest[pos])
            if i != current:
                pos += 1
        st.entries = new  # pl_idx unchanged: the current entry stayed put

    def _restore_original(self) -> None:
        st = self.state
        if st.orig_entries is None:
            return
        current = st.entries[st.pl_idx] if st.entries else None
        st.entries = list(st.orig_entries)
        st.orig_entries = None
        if current is not None:
            for i, e in enumerate(st.entries):
                if e is current:
                    st.pl_idx = i
                    break

    # --------------------------------------------------------------- dispatch
    def _dispatch(self, raw: bytes, peer) -> None:
        for line in raw.decode(errors="replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "command" not in msg:
                continue
            target = msg.get("nvr-reply-to")
            if target:
                self.peers.add(target)
            rid = msg.get("request_id")
            with self.lock:
                error, extra = self._exec(msg["command"])
            reply = {"error": error, "request_id": rid}
            reply.update(extra)
            data = json.dumps(reply, separators=(",", ":")).encode()
            if self.sock is None:
                return
            dst = target or peer
            if not dst:
                return
            try:
                self.sock.sendto(data, dst)
            except OSError:
                if target:
                    self.peers.discard(target)

    def _exec(self, cmd) -> "tuple[str, dict]":
        """Return (error, extras) in the mpv response shape."""
        if not isinstance(cmd, list) or not cmd:
            return "error", {}
        head = str(cmd[0])
        st = self.state

        if head == "get_version":
            return "success", {"data": VERSION}

        if head == "get_property":
            if len(cmd) < 2:
                return "error", {}
            names = cmd[1] if isinstance(cmd[1], list) else [cmd[1]]
            snap = st.snapshot()
            if len(names) == 1:
                name = str(names[0])
                if name not in snap:
                    return "error", {"error": "property-not-found"}
                return "success", {"data": snap[name]}
            data = {}
            for n in names:
                n = str(n)
                if n not in snap:
                    return "error", {"error": "property-not-found"}
                data[n] = snap[n]
            return "success", {"data": data}

        if head == "set_property":
            if len(cmd) < 3:
                return "error", {}
            name, val = str(cmd[1]), cmd[2]
            if name in READONLY_PROPS:
                return "error", {"error": "readonly-property"}
            if name not in KNOWN_PROPS:
                return "error", {"error": "property-not-found"}
            if isinstance(val, bool):
                val = int(val)
            if name == "pause":
                st.pause_flag = bool(val)
            elif name == "volume":
                st.volume = float(val)
            elif name == "time-pos":
                st.time_pos = float(val)
            elif name == "shuffle":
                wanted = int(bool(val))
                if wanted and st.shuffle == 0:
                    self._shuffle_physical()
                elif not wanted and st.shuffle == 1:
                    self._restore_original()
                st.shuffle = wanted
            elif name in PROP_FIELDS:
                # mpv spells these with dashes; the State fields use
                # underscores — a bare setattr(name) would create a dead
                # dict key that snapshot() never reads.
                setattr(st, PROP_FIELDS[name], int(val))
            else:
                return "error", {"error": "readonly-property"}
            return "success", {}

        if head == "loadfile":
            if len(cmd) < 2:
                return "error", {}
            uri = str(cmd[1])
            flag = str(cmd[2]) if len(cmd) > 2 else ""
            id_ = None
            # trailing-string argument may carry the entry id (defensive
            # parity: the A4 controller tags ids via playlist-add).
            for tok in reversed(cmd[2:]):
                if isinstance(tok, str) and tok not in ("replace", "append"):
                    id_ = tok
                    break
            was_idle = st.idle
            if flag == "replace":
                st.entries = [Entry(uri, id_, self.default_duration)]
                st.pl_idx = 0
            elif flag == "append":
                st.entries.append(Entry(uri, id_, self.default_duration))
                st.pl_idx = len(st.entries) - 1
            else:  # default: play at current position (seed if empty)
                if not st.entries:
                    st.entries = [Entry(uri, id_, self.default_duration)]
                    st.pl_idx = 0
                elif 0 <= st.pl_idx < len(st.entries):
                    st.entries[st.pl_idx] = Entry(
                        uri, id_, self.default_duration)
                else:
                    st.entries.append(
                        Entry(uri, id_, self.default_duration))
                    st.pl_idx = len(st.entries) - 1
            self._load_entry(st.entries[st.pl_idx])
            if was_idle:
                self._emit({"event": "idle", "idle": False})
            return "success", {}

        if head in ("pl_add", "playlist-add"):
            if len(cmd) < 2:
                return "error", {}
            uri = str(cmd[1])
            id_ = None
            idx = None
            if len(cmd) > 3:
                id_ = cmd[3]
            if len(cmd) == 3 and not isinstance(cmd[2], (int, float)):
                id_ = cmd[2]
            elif len(cmd) > 2:
                idx = int(cmd[2])
            entry = Entry(uri, id_, self.default_duration)
            if idx is None or idx < 0:
                st.entries.append(entry)
            else:
                idx = max(0, min(idx, len(st.entries)))
                st.entries.insert(idx, entry)
            return "success", {}

        if head in ("pl_clear", "playlist-clear"):
            st.entries = []
            st.orig_entries = None
            st.pl_idx = 0
            st._plays_left.clear()
            if not st.idle:
                st.idle = True
                st.pause_flag = False
                st.time_pos = 0.0
                st.media_title = ""
                st.current_url = ""
                self._emit({"event": "idle", "idle": True})
            return "success", {}

        if head in ("pl_play", "playlist-play"):
            if len(cmd) < 2 or not isinstance(cmd[1], int) or not st.entries:
                return "error", {"error": "no-playlist"}
            idx = max(0, min(int(cmd[1]), len(st.entries) - 1))
            self._goto(idx)
            return "success", {}

        if head == "playlist-shuffle":
            self._shuffle_physical()
            return "success", {}

        if head == "playlist-move":
            if (len(cmd) < 3 or not isinstance(cmd[1], int)
                    or not isinstance(cmd[2], int) or not st.entries):
                return "error", {}
            a, b = cmd[1], cmd[2]
            if not (0 <= a < len(st.entries)):
                return "error", {}
            entry = st.entries.pop(a)
            st.entries.insert(max(0, min(b, len(st.entries))), entry)
            return "success", {}

        if head == "playlist-delete":
            if len(cmd) < 2 or not isinstance(cmd[1], int) or not st.entries:
                return "error", {}
            idx = cmd[1]
            if not (0 <= idx < len(st.entries)):
                return "error", {}
            st.entries.pop(idx)
            if st.pl_idx >= len(st.entries):
                st.pl_idx = max(0, len(st.entries) - 1)
            return "success", {}

        if head == "seek":
            if len(cmd) < 2:
                return "error", {}
            try:
                amt = float(cmd[1])
            except (TypeError, ValueError):
                return "error", {}
            unit = str(cmd[2]) if len(cmd) > 2 else "relative"
            dur = st.current_entry
            dur = dur.duration if dur is not None else 0.0
            if unit == "milliseconds":
                amt /= 1000.0
            if unit == "percent":
                target = (amt / 100.0) * dur if dur else 0.0
            elif unit == "absolute":
                target = amt
            else:  # relative (default)
                target = st.time_pos + amt
            if dur > 0:
                target = max(0.0, min(target, dur))
            else:
                target = max(0.0, target)
            st.time_pos = target
            return "success", {}

        if head in ("playlist-next", "playlist-prev"):
            if st.idle and not st.entries:
                # M0' legacy: engine not loaded at all
                return "error", {"error": "no-playlist"}
            n = len(st.entries)
            if n == 0:
                # M0' legacy single-file path (entries not seeded)
                delta = 1 if head == "playlist-next" else -1
                st.pl_idx = max(0, st.pl_idx + delta)
                st.time_pos = 0.0
                if st.media_title:
                    st.media_title = f"{st.media_title} (#{st.pl_idx})"
                return "success", {}
            delta = 1 if head == "playlist-next" else -1
            nxt = st.pl_idx + delta
            if delta > 0:
                if nxt >= n:
                    if st.loop_playlist < 0:
                        nxt = 0
                    else:
                        # end of playlist, no loop: engine stops (goes idle)
                        st.idle = True
                        st.pause_flag = False
                        st.time_pos = 0.0
                        self._emit({"event": "idle", "idle": True})
                        return "success", {}
            else:
                if nxt < 0:
                    nxt = 0 if st.loop_playlist >= 0 else n - 1
            self._goto(nxt)
            return "success", {}

        if head == "stop":
            if not st.idle:
                st.idle = True
                st.pause_flag = False
                st.time_pos = 0.0
                self._emit({"event": "idle", "idle": True})
            return "success", {}

        if head == "quit":
            self.stop.set()
            return "success", {}

        if head == "get":
            return "success", {"data": st.snapshot()}

        return "error", {"error": f"unsupported: {head}"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--socket", default="/dev/shm/navidrome-remote-mock-ipc")
    ap.add_argument("--tick-hz", type=float, default=4.0)
    ap.add_argument("--duration", type=float, default=180.0,
                    help="synthesized duration (s) per loaded entry")
    ap.add_argument("--seed", type=int, default=None,
                    help="rng seed (deterministic shuffle for tests)")
    args = ap.parse_args()
    sink = Sink(args.socket, tick_hz=args.tick_hz,
                duration=args.duration, seed=args.seed)
    signal.signal(signal.SIGTERM, lambda *_: sink.stop.set())
    signal.signal(signal.SIGINT, lambda *_: sink.stop.set())
    try:
        sink.start()
    finally:
        sink.shutdown()


if __name__ == "__main__":
    main()
