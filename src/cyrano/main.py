"""Application entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from cyrano.config import ConfigError, Settings, load_settings
from cyrano.health import HEARTBEAT_INTERVAL, heartbeat_loop
from cyrano.logging import configure_logging

logger = logging.getLogger("cyrano")


async def run(
    settings: Settings,
    stop: asyncio.Event | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
) -> None:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info("cyrano started", extra={"service": "app", **settings.safe_summary()})
    heartbeat = asyncio.create_task(heartbeat_loop(settings.heartbeat_path, heartbeat_interval))
    try:
        await stop.wait()
    finally:
        logger.info("cyrano shutting down", extra={"service": "app"})
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        settings.heartbeat_path.unlink(missing_ok=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        logger.info("cyrano stopped", extra={"service": "app"})


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings.library_log_levels)
    asyncio.run(run(settings))
    return 0
