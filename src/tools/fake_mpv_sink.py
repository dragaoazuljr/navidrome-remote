#!/usr/bin/env python3
"""Mock mpv IPC sink — the C1 stand-in for the real playback engine.

Speaks the same mpv JSON-IPC protocol as real mpv (newline-delimited JSON
over an AF_UNIX datagram socket) so that navidrome_remote.ipc.MpvEngine is
exercised byte-for-byte on the dev-workspace, where neither mpv nor an audio
device exists (C1 per spec v1.2: "SEM validação de áudio").

Usage:
  python3 tools/fake_mpv_sink.py --socket /dev/shm/navidrome-remote-mock-ipc

Behaviors emulated:
  * get_version; get_property (single name, list of names → dict data);
  * set_property (pause, volume, time-pos, ...);
  * loadfile <url> [replace] — starts "playback" (ticker advances time-pos);
  * playlist-next / playlist-prev / stop / quit;
  * events: idle-state flips and property-change (time-pos), like real mpv.

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


VERSION = "0.1.0-fake-mpv-sink"


class State:
    def __init__(self) -> None:
        self.idle = True
        self.pause_flag = False
        self.volume = 100.0
        self.time_pos = 0.0
        self.media_title = ""
        self.playlist_index = 0
        self.current_url = ""

    def playing(self) -> bool:
        return not self.idle and not self.pause_flag

    def playback_status(self) -> str:
        if self.idle:
            return "stopped"
        return "paused" if self.pause_flag else "playing"

    def snapshot(self) -> dict:
        return {
            "idle": self.idle,
            "core-idle": self.idle,
            "playback-status": self.playback_status(),
            "pause": self.pause_flag,
            "volume": self.volume,
            "time-pos": round(self.time_pos, 3),
            "media-title": self.media_title,
            "playlist-index": self.playlist_index,
            "filename": self.current_url,
        }


KNOWN_PROPS = set(State().snapshot().keys())


class Sink:
    def __init__(self, socket_path: str, tick_hz: float = 4.0) -> None:
        self.socket_path = socket_path
        self.tick_hz = tick_hz
        self.state = State()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.peers: "set" = set()  # senders we know (real mpv multicasts to registered viewers)
        self.sock: "socket.socket | None" = None

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
            # CPython yields NO sender address for AF_UNIX datagrams (real mpv
            # uses C recvmsg, which does), so our client declares the reply
            # target in-band via `nvr-reply-to` — ignored by real mpv.
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
                return  # no address recoverable; nothing to do
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
            if name not in KNOWN_PROPS or name in ("playback-status", "media-title", "playlist-index", "filename"):
                return "error", {"error": "property-not-found"}
            if isinstance(val, bool):
                if name == "pause":
                    st.pause_flag = val
                elif name == "idle":
                    st.idle = val
                else:
                    return "error", {"error": "readonly-property"}
            elif name == "volume":
                st.volume = float(val)
            elif name == "time-pos":
                st.time_pos = float(val)
            else:
                return "error", {"error": "readonly-property"}
            return "success", {}

        if head == "loadfile":
            if len(cmd) < 2:
                return "error", {}
            url = str(cmd[1])
            was_idle = st.idle
            st.idle = False
            st.current_url = url
            st.time_pos = 0.0
            base = url.split("?", 1)[0].rsplit("/", 1)[-1]
            st.media_title = base or url[:64]
            if was_idle:
                self._emit({"event": "idle", "idle": False})
            self._emit({"event": "file-loaded", "filename": url, "media-title": st.media_title})
            return "success", {}

        if head in ("playlist-next", "playlist-prev"):
            if st.idle:
                return "error", {"error": "no-playlist"}
            delta = 1 if head == "playlist-next" else -1
            st.playlist_index = max(0, st.playlist_index + delta)
            st.time_pos = 0.0
            if st.media_title:
                st.media_title = f"{st.media_title} (#{st.playlist_index})"
            return "success", {}

        if head == "stop":
            if not st.idle:
                st.idle = True
                st.pause_flag = False
                st.time_pos = 0.0
                st.current_url = ""
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
