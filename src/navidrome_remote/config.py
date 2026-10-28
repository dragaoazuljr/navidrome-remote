"""Runtime configuration, exclusively from the environment (EnvironmentFile).

One line flips the upstream route (addendum v1.2 / spec v1.2 D1):

  P1 — LAN crua:   NVRA_UPSTREAM=192.168.0.200:4533
  P2 — tailnet:    NVRA_UPSTREAM=100.90.124.50:80
                   NVRA_UPSTREAM_VHOST=navidrome.lan

Nothing else in the codebase knows which route is active; the vhost is what
gets rewritten into the forwarded `Host` header.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Fatal, operator-visible misconfiguration (process exits with message)."""


def _hostport(value: str, what: str) -> tuple[str, int]:
    if not value:
        raise ConfigError(f"{what}: valor vazio")
    host, sep, port_s = value.rpartition(":")
    if not sep or not host:
        raise ConfigError(f"{what}: formato esperado host:port (recebi {value!r})")
    try:
        port = int(port_s)
    except ValueError:
        raise ConfigError(f"{what}: porta não numérica ({port_s!r})")
    if not (0 < port <= 65535):
        raise ConfigError(f"{what}: porta fora do intervalo ({port})")
    return host, port


@dataclass
class Config:
    listen_addr: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    upstream_vhost: str = ""        # empty -> Host rewritten to upstream host:port
    mpv_socket: str = "/dev/shm/mpv-ipc"
    spy_file: str = "/var/lib/navidrome-remote/spy.ndjson"
    api_token: str = ""              # L0 (optional) static token — M1: see gateway
    nav_user: str = ""               # Subsonic service account (never in KB)
    nav_pass: str = ""
    state_file: str = "/var/lib/navidrome-remote/state.json"  # M1 queue/state store
    max_request_bytes: int = 16 * 1024 * 1024   # upload/cover-art ceiling
    max_html_bytes: int = 4 * 1024 * 1024       # SPA document ceiling
    keepalive_idle: float = 60.0                # s to wait for the next request on a
                                                # keepalive connection before closing
                                                # (protects the 1GB box from half-open
                                                # clients; also bounds aclose()/wait_closed)

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "Config":
        env = os.environ if environ is None else environ
        try:
            listen_addr = env.get("NVRA_LISTEN_ADDR", "0.0.0.0")
            listen_port = int(env.get("NVRA_LISTEN_PORT", "80"))
            uhost, uport = _hostport(
                env.get("NVRA_UPSTREAM", "192.168.0.200:4533"), "NVRA_UPSTREAM"
            )
            cfg = cls(
                listen_addr=listen_addr,
                listen_port=listen_port,
                upstream_host=uhost,
                upstream_port=uport,
                upstream_vhost=env.get("NVRA_UPSTREAM_VHOST", "").strip(),
                mpv_socket=env.get("NVRA_MPV_SOCKET", "/dev/shm/mpv-ipc"),
                spy_file=env.get("NVRA_SPY_FILE", "/var/lib/navidrome-remote/spy.ndjson"),
                api_token=env.get("NVRA_API_TOKEN", ""),
                nav_user=env.get("NVRA_NAV_USER", ""),
                nav_pass=env.get("NVRA_NAV_PASS", ""),
                state_file=env.get("NVRA_STATE_FILE",
                                   "/var/lib/navidrome-remote/state.json"),
                max_request_bytes=int(env.get("NVRA_MAX_REQ_BYTES", "16777216")),
                max_html_bytes=int(env.get("NVRA_MAX_HTML_BYTES", "4194304")),
                keepalive_idle=float(env.get("NVRA_KEEPALIVE_IDLE", "60")),
            )
        except ValueError as exc:
            raise ConfigError(f"valor numérico inválido: {exc}") from exc
        return cfg

    # ------------------------------------------------------------------ host
    @property
    def vhost_authority(self) -> str:
        """Authority used in the forwarded Host header."""
        vh = self.upstream_vhost
        if not vh:
            return self.upstream_authority
        if vh.count(":") == 0:  # vhost without port: upstream port implies it
            if self.upstream_port in (80, 443):
                return vh
            return f"{vh}:{self.upstream_port}"
        return vh

    @property
    def upstream_authority(self) -> str:
        if self.upstream_port in (80, 443):
            return self.upstream_host
        return f"{self.upstream_host}:{self.upstream_port}"

    @property
    def upstream_base_url(self) -> str:
        scheme = "https" if self.upstream_port == 443 else "http"
        if self.upstream_vhost:
            auth = self.upstream_vhost
            if ":80" not in auth and ":443" not in auth and self.upstream_port not in (80, 443):
                auth = f"{auth}:{self.upstream_port}"
            return f"{scheme}://{auth}"
        return f"{scheme}://{self.upstream_authority}"
