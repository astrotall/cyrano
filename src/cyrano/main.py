"""Application entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Callable, Sequence

from cyrano.config import ConfigError, Settings, load_settings
from cyrano.db.repositories import SqlContactRepository, SqlMessageRepository
from cyrano.db.session import build_engine, build_session_factory
from cyrano.health import HEARTBEAT_INTERVAL, heartbeat_loop
from cyrano.logging import configure_logging
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.intake import IntakeService
from cyrano.userbot.client import build_client, ensure_session_exists
from cyrano.userbot.errors import UserbotError
from cyrano.userbot.login import run_login
from cyrano.userbot.protocols import UserbotClient
from cyrano.userbot.runner import Userbot

logger = logging.getLogger("cyrano")

LOGIN_COMMAND = "login"
EXIT_USERBOT_ERROR = 1
EXIT_CONFIG_ERROR = 2
LOGIN_CANCELLED_MESSAGE = "Login cancelled. Nothing was saved."


async def run(
    settings: Settings,
    stop: asyncio.Event | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
    client_factory: Callable[[Settings], UserbotClient] = build_client,
) -> None:
    ensure_session_exists(settings.userbot_session_path)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info("cyrano started", extra={"service": "app", **settings.safe_summary()})
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    intake = IntakeService(
        SqlContactRepository(session_factory),
        SqlMessageRepository(session_factory),
        InMemoryContactEvents(),
    )
    userbot = Userbot(client_factory(settings), intake)
    heartbeat = asyncio.create_task(heartbeat_loop(settings.heartbeat_path, heartbeat_interval))
    try:
        await userbot.start(stop)
        await userbot.run_until(stop)
    finally:
        logger.info("cyrano shutting down", extra={"service": "app"})
        await userbot.stop()
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        settings.heartbeat_path.unlink(missing_ok=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        await engine.dispose()
        logger.info("cyrano stopped", extra={"service": "app"})


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m cyrano", description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser(LOGIN_COMMAND, help="log the second account in and save the session")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_CONFIG_ERROR
    configure_logging(settings.log_level, settings.library_log_levels)
    try:
        if args.command == LOGIN_COMMAND:
            asyncio.run(run_login(settings))
        else:
            asyncio.run(run(settings))
    except UserbotError as exc:
        print(exc, file=sys.stderr)
        return EXIT_USERBOT_ERROR
    except EOFError, KeyboardInterrupt:
        if args.command != LOGIN_COMMAND:
            raise
        print(LOGIN_CANCELLED_MESSAGE, file=sys.stderr)
        return EXIT_USERBOT_ERROR
    return 0
