"""Entry point — `python3 -m navidrome_remote` (1 systemd unit, Restart=always).

Everything is configured through the environment (EnvironmentFile per spec
v1.2): NVRA_LISTEN_ADDR / NVRA_LISTEN_PORT / NVRA_UPSTREAM /
NVRA_UPSTREAM_VHOST / NVRA_MPV_SOCKET / NVRA_SPY_FILE / NVRA_STATE_FILE /
NVRA_API_TOKEN / NVRA_NAV_USER / NVRA_NAV_PASS.

Fail-fast philosophy: misconfiguration must never mean "the service silently
half-works". Bad env -> loud error on stderr + nonzero exit -> systemd
restarts -> stays visible in the unit journal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from . import NAVIDROME_ANCHOR, SERVICE_NAME, SERVICE_VERSION
from .api import Api
from .config import Config, ConfigError
from .gateway import SessionGate
from .ipc import MpvEngine
from .proxy import ProxyServer
from .queue import QueueStore, apply_to_engine
from .spy import Spy


def _banner(cfg: Config, engine: MpvEngine, store: QueueStore) -> None:
    route = "custom"
    if (cfg.upstream_host, cfg.upstream_port) == ("192.168.0.200", 4533):
        route = "P1-LAN-crua"
    elif cfg.upstream_vhost:
        route = f"P2-ts-gw (vhost={cfg.upstream_vhost})"
    log = logging.getLogger(SERVICE_NAME)
    log.info(
        "%s %s up — listen=%s:%s upstream=%s (%s) vhost=%s socket=%s spy=%s state=%s anchor=%s api-token=%s gate=session-3layer",
        SERVICE_NAME,
        SERVICE_VERSION,
        cfg.listen_addr,
        cfg.listen_port,
        f"{cfg.upstream_host}:{cfg.upstream_port}",
        route,
        cfg.vhost_authority,
        cfg.mpv_socket,
        cfg.spy_file,
        cfg.state_file,
        NAVIDROME_ANCHOR,
        "set" if cfg.api_token else "off",
    )
    if store.pending_restore:
        log.info(
            "estado persistente em memória (%d itens) — aplicação ao motor quando disponível",
            len(store.items),
        )
    if not engine.alive:
        log.warning(
            "motor de playback FORA DO AR (socket %s ausente/sem resposta) — "
            "API /api/v1/* reportará engine-down até a unidade mpv (ou o mock) subir",
            cfg.mpv_socket,
        )


def _load_bridge_asset() -> bytes:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "bridge.js")
    data = open(p, "rb").read()
    if not data:
        raise ConfigError("asset bridge.js vazio")
    return data


async def _restore_loop(store: QueueStore, engine: MpvEngine, spy: Spy,
                        stop_event: asyncio.Event) -> None:
    """Lazy best-effort application of persisted queue/state to the engine.

    Retries every 5 s while the engine is down and `pending_restore` holds;
    the engine's ack is the receipt (apply_to_engine clears the flag). A
    failure here NEVER propagates into a client request.
    """
    while not stop_event.is_set():
        if store.pending_restore:
            try:
                if engine.alive:
                    await apply_to_engine(store, engine, spy)
            except Exception as exc:
                print(
                    "[%s] restore tentou e falhou (%s) — novo ciclo em 5s"
                    % (SERVICE_NAME, type(exc).__name__),
                    file=sys.stderr,
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            break
        except asyncio.TimeoutError:
            pass


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
        store = QueueStore(cfg, spy)
        gate = SessionGate(cfg, spy)
        api = Api(cfg, spy, engine, store, gate)
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

    server_started = loop.run_until_complete(server.start())
    _banner(cfg, engine, store)

    restore_task = loop.create_task(_restore_loop(store, engine, spy, stop_event))
    try:
        loop.run_until_complete(stop_event.wait())
    finally:
        restore_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            loop.run_until_complete(restore_task)
        store.save()
        loop.run_until_complete(server.aclose())
        server_started.close()
        with contextlib.suppress(Exception):
            loop.run_until_complete(server_started.wait_closed())
        engine.close()
        loop.close()
        logging.getLogger(SERVICE_NAME).info("shutdown limpo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
