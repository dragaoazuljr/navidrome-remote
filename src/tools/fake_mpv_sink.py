#!/usr/bin/env python3
"""Mock mpv IPC sink — the C1 stand-in for the real playback engine.

Speaks the same mpv JSON-IPC protocol as real mpv (newline-delimited
JSON over an AF_UNIX datagram socket) so that navidrome_remote.ipc.
MpvEngine is exercised byte-for-byte on the dev-workspace, where
neither mpv nor an audio device exists (C1 per spec v1.2: "SEM
validação de áudio").

Usage:
  python3 tools/fake_mpv_sink.py --socket /dev/shm/navidrome-remote-mock-ipc

Behaviors emulated:
  * get_version; get_property (single name, list of names -> dict);
  * set_property (pause, volume, time-pos, shuffle, loop-file,
    loop-playlist, ...);
  * loadfile <uri> [replace|append] — playlist-aware (M1):
      replace   -> items=[uri], play index 0
      append    -> items += [uri], play last
      default   -> play uri at current position (seed playlist if empty)
  * pl_add / playlist-add [index], pl_clear / playlist-clear,
    pl_play / playlist-play, playlist-move, playlist-delete;
  * playlist-next / playlist-prev — playlist aware (loop-playlist wraps,
    end-of-list stops like real mpv) with the M0' single-file legacy
    behaviour preserved for an empty playlist;
  * stop / quit;
  * events: idle-state flips, file-loaded, property-change (time-pos).

On 2W/C3 this program is simply replaced by:
  mpv --idle=yes --really-quiet --input-ipc-server=<same socket>
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import threading
import time


VERSION = "0.2.0-fake-mpv-sink"


class State:
    def __init__(self) -> None:
        self.idle = True
        self.pause_flag = False
        self.volume = 100.0
        self.time_pos = 0.0
        self.media_title = ""
        self.current_url = ""
        # M1: playlist model
        self.items: "list[str]" = []
        self.pl_idx = 0
        self.shuffle = 0
        self.loop_file = 0
        self.loop_playlist = 0

    def playing(self) -> bool:
        return not self.idle and not self.pause_flag

    def playback_status(self) -> str:
        if self.idle:
            return "stopped"
        return "paused" if self.pause_flag else "playing"

    @staticmethod
    def _title_of(uri: str) -> str:
        base = uri.split("?", 1)[0].rsplit("/", 1)[-1]
        return base or uri[:64]

    def snapshot(self) -> dict:
        return {
            "idle": self.idle,
            "core-idle": self.idle,
            "playback-status": self.playback_status(),
            "pause": self.pause_flag,
            "volume": self.volume,
            "time-pos": round(self.time_pos, 3),
            "media-title": self.media_title,
            "playlist-size": len(self.items),
            "playlist-index": self.pl_idx,
            "filename": self.current_url,
            "shuffle": self.shuffle,
            "loop-file": self.loop_file,
            "loop-playlist": self.loop_playlist,
        }


KNOWN_PROPS = set(State().snapshot().keys())

# mpv property name (dash spelling) -> State field (underscore spelling),
# for writable properties that have no explicit assignment branch above.
PROP_FIELDS = {"loop-file": "loop_file", "loop-playlist": "loop_playlist"}


class Sink:
    def __init__(self, socket_path: str, tick_hz: float = 4.0) -> None:
        self.socket_path = socket_path
        self.tick_hz = tick_hz
        self.state = State()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.peers: "set" = set()
        self.sock: "socket | None" = None

    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.socket_path)
        self.sock.settimeout(0.25)
        print(f"[fake-mpv-sink] listening on {self.socket_path} (version={VERSION})", flush=True)
        ticker = threading.Thread(target=self._tick_loop, daemon=True)
        ticker.start()
        while not self.stop.is_set():
            try:
                raw, _peer = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            self._dispatch(raw, _peer)

    # ------------------------------------------------------------------ ticks
    def _tick_loop(self) -> None:
        last = time.monotonic()
        while not self.stop.is_set():
            now = time.monotonic()
            dt = now - last
            last = now
            if self.state.playing():
                self.state.time_pos += dt
                if int(self.state.time_pos * self.tick_hz) > int((self.state.time_pos - dt) * self.tick_hz):
                    self._emit(
                        {
                            "event": "property-change",
                            "property": "time-pos",
                            "data": round(self.state.time_pos, 3),
                        }
                    )
            time.sleep(0.05)

    def _emit(self, obj: dict) -> None:
        if self.sock is None or not self.peers:
            return
        data = json.dumps(obj, separators=(",", ":")).encode()
        for peer in list(self.peers):
            try:
                self.sock.sendto(data, peer)
            except OSError:
                self.peers.discard(peer)

    def _load_uri(self, uri: str) -> None:
        st = self.state
        st.idle = False
        st.current_url = uri
        st.time_pos = 0.0
        st.media_title = State._title_of(uri)
        self._emit({"event": "file-loaded", "filename": uri,
                    "media-title": st.media_title})

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
            if name not in KNOWN_PROPS or name in ("playback-status", "media-title",
                                                   "playlist-index", "filename",
                                                   "playlist-size", "idle",
                                                   "core-idle"):
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
                st.shuffle = int(bool(val))
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
            was_idle = st.idle
            if flag == "replace":
                st.items = [uri]
                st.pl_idx = 0
            elif flag == "append":
                st.items.append(uri)
                st.pl_idx = len(st.items) - 1
            else:  # default: play at current position
                if not st.items:
                    st.items = [uri]
                    st.pl_idx = 0
                elif 0 <= st.pl_idx < len(st.items):
                    st.items[st.pl_idx] = uri
                else:
                    st.items.append(uri)
                    st.pl_idx = len(st.items) - 1
            self._load_uri(uri)
            if was_idle:
                self._emit({"event": "idle", "idle": False})
            return "success", {}

        if head in ("pl_add", "playlist-add"):
            if len(cmd) < 2:
                return "error", {}
            uri = str(cmd[1])
            if len(cmd) > 2 and isinstance(cmd[2], int):
                idx = max(0, min(cmd[2], len(st.items)))
                st.items.insert(idx, uri)
            else:
                st.items.append(uri)
            return "success", {}

        if head in ("pl_clear", "playlist-clear"):
            st.items = []
            st.pl_idx = 0
            return "success", {}

        if head in ("pl_play", "playlist-play"):
            if len(cmd) < 2 or not isinstance(cmd[1], int) or not st.items:
                return "error", {"error": "no-playlist"}
            idx = max(0, min(int(cmd[1]), len(st.items) - 1))
            st.pl_idx = idx
            self._load_uri(st.items[idx])
            return "success", {}

        if head == "playlist-move":
            if (len(cmd) < 3 or not isinstance(cmd[1], int)
                    or not isinstance(cmd[2], int) or not st.items):
                return "error", {}
            a, b = cmd[1], cmd[2]
            if not (0 <= a < len(st.items)):
                return "error", {}
            uri = st.items.pop(a)
            st.items.insert(max(0, min(b, len(st.items))), uri)
            return "success", {}

        if head == "playlist-delete":
            if len(cmd) < 2 or not isinstance(cmd[1], int) or not st.items:
                return "error", {}
            idx = cmd[1]
            if not (0 <= idx < len(st.items)):
                return "error", {}
            st.items.pop(idx)
            if st.pl_idx >= len(st.items):
                st.pl_idx = max(0, len(st.items) - 1)
            return "success", {}

        if head in ("playlist-next", "playlist-prev"):
            if st.idle and not st.items:
                # M0' legacy: engine not loaded at all
                return "error", {"error": "no-playlist"}
            n = len(st.items)
            if n == 0:
                # M0' legacy single-file path (items not seeded)
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
                        # end of playlist, no loop: mpv stops (goes idle)
                        st.idle = True
                        st.pause_flag = False
                        st.time_pos = 0.0
                        self._emit({"event": "idle", "idle": True})
                        return "success", {}
            else:
                if nxt < 0:
                    nxt = 0 if st.loop_playlist >= 0 else n - 1
            st.pl_idx = nxt
            self._load_uri(st.items[nxt])
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
    args = ap.parse_args()
    sink = Sink(args.socket, tick_hz=args.tick_hz)
    signal.signal(signal.SIGTERM, lambda *_: sink.stop.set())
    signal.signal(signal.SIGINT, lambda *_: sink.stop.set())
    try:
        sink.start()
    finally:
        if sink.sock is not None:
            sink.sock.close()
        if os.path.exists(sink.socket_path):
            os.unlink(sink.socket_path)


if __name__ == "__main__":
    main()
