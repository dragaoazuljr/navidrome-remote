"""Entry point — `python3 -m navidrome_remote` (1 systemd unit, Restart=always).

Everything is configured through the environment (EnvironmentFile per spec
v1.2): NVRA_LISTEN_ADDR / NVRA_LISTEN_PORT / NVRA_UPSTREAM /
NVRA_UPSTREAM_VHOST / NVRA_MPV_SOCKET / NVRA_SPY_FILE / NVRA_API_TOKEN /
NVRA_NAV_USER / NVRA_NAV_PASS.

Fail-fast philosophy: misconfiguration must never mean "the service silently
half-works". Bad env -> loud error on stderr + nonzero exit -> systemd
restarts -> stays visible in the unit journal.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
import signal
import sys
from pathlib import Path

from . import NAVIDROME_ANCHOR, SERVICE_NAME, SERVICE_VERSION
from .api import Api
from .config import Config, ConfigError
from .ipc import MpvEngine
from .proxy import ProxyServer
from .spy import Spy


def _banner(cfg: Config, engine: MpvEngine) -> None:
    route = "custom"
    if (cfg.upstream_host, cfg.upstream_port) == ("192.168.0.200", 4533):
        route = "P1-LAN-crua"
    elif cfg.upstream_vhost:
        route = f"P2-ts-gw (vhost={cfg.upstream_vhost})"
    log = logging.getLogger(SERVICE_NAME)
    log.info(
        "%s %s up — listen=%s:%s upstream=%s (%s) vhost=%s socket=%s spy=%s anchor=%s api-token=%s",
        SERVICE_NAME,
        SERVICE_VERSION,
        cfg.listen_addr,
        cfg.listen_port,
        f"{cfg.upstream_host}:{cfg.upstream_port}",
        route,
        cfg.vhost_authority,
        cfg.mpv_socket,
        cfg.spy_file,
        NAVIDROME_ANCHOR,
        "set" if cfg.api_token else "off",
    )
    if not engine.alive:
        log.warning(
            "motor de playback FORA DO AR (socket %s ausente/sem resposta) — "
            "API /api/v1/* reportará engine-down até a unidade mpv (ou o mock) subir",
            cfg.mpv_socket,
        )


def _load_bridge_asset() -> bytes:
    p = Path(__file__).resolve().parent / "assets" / "bridge.js"
    data = p.read_bytes()
    if not data:
        raise ConfigError("asset bridge.js vazio")
    return data


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"[{SERVICE_NAME}] FATAL: configuração inválida: {exc}", file=sys.stderr)
        return 1

    try:
        spy = Spy(cfg.spy_file)
        engine = MpvEngine(cfg.mpv_socket)
        api = Api(cfg, spy, engine)
        bridge = _load_bridge_asset()
        server = ProxyServer(cfg, spy, api, bridge)
    except ConfigError as exc:
        print(f"[{SERVICE_NAME}] FATAL: {exc}", file=sys.stderr)
        return 1

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    def _sig(signame: str) -> None:
        logging.getLogger(SERVICE_NAME).info("signal %s — encerrando", signame)
        stop_event.set()

    for s in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(s, _sig, s.name)

    srv = loop.run_until_complete(server.start())
    _banner(cfg, engine)

    try:
        loop.run_until_complete(stop_event.wait())
    finally:
        loop.run_until_complete(server.aclose())
        srv.close()
        try:
            loop.run_until_complete(srv.wait_closed())
        except Exception:
            pass
        engine.close()
        loop.close()
        logging.getLogger(SERVICE_NAME).info("shutdown limpo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
