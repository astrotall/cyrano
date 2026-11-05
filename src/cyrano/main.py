"""Application entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from cyrano.bot.app import ControlBot
from cyrano.bot.errors import ControlBotError, PollingFailed
from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.config import ConfigError, Settings, load_settings
from cyrano.convert_cli import add_convert_parser, convert_request, run_convert
from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlPersonaRepository,
    SqlScheduleProfileRepository,
    SqlSenderStateRepository,
    SqlStyleExampleRepository,
)
from cyrano.db.session import build_engine, build_session_factory
from cyrano.domain.enums import LLMStep
from cyrano.domain.errors import LLMError
from cyrano.domain.models import ContactReadyEvent, NewContactEvent
from cyrano.health import HEARTBEAT_INTERVAL, heartbeat_loop
from cyrano.llm.factory import LLMClientFactory
from cyrano.logging import configure_logging
from cyrano.outbox_cli import (
    OutboxCommands,
    OutboxRequest,
    OutboxUsageError,
    add_outbox_parser,
    outbox_request,
)
from cyrano.reply_cli import (
    ReplyPreviewCommand,
    ReplyPreviewRequest,
    add_reply_preview_parser,
    reply_preview_request,
)
from cyrano.schedule_cli import (
    ScheduleCommands,
    ScheduleRequest,
    ScheduleUsageError,
    add_schedule_parser,
    schedule_request,
)
from cyrano.services.card_actions import CardActions
from cyrano.services.card_announcer import CardAnnouncer
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.humanization import HumanizationPlanner, ScheduleService
from cyrano.services.intake import IntakeService
from cyrano.services.llm_check import check_llm
from cyrano.services.llm_client import LLMClient
from cyrano.services.manual_intervention import ManualMessageDetector
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.outbox_service import OutboxService
from cyrano.services.pause_actions import PauseActions
from cyrano.services.persona_service import PersonaService
from cyrano.services.profile_collector import ProfileCollector
from cyrano.services.prompt_builder import PromptBuilder
from cyrano.services.reply_generator import ReplyGenerator
from cyrano.services.schedule_files import ScheduleImporter
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from cyrano.services.style_examples import ExampleSelector
from cyrano.services.style_import import ExamplesImporter, PersonaImporter
from cyrano.style_cli import StyleCommands, StyleRequest, add_style_parsers, style_request
from cyrano.userbot.client import UserbotAccess, build_access, ensure_session_exists
from cyrano.userbot.errors import UserbotError
from cyrano.userbot.leomatch_source import TelethonLeomatchSource
from cyrano.userbot.login import run_login
from cyrano.userbot.profile_source import TelethonProfileSource
from cyrano.userbot.runner import Userbot

logger = logging.getLogger("cyrano")

LOGIN_COMMAND = "login"
LLM_CHECK_COMMAND = "llm-check"
EXIT_USERBOT_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_LLM_ERROR = 1
EXIT_STYLE_ERROR = 1
EXIT_REPLY_PREVIEW_ERROR = 1
EXIT_OUTBOX_ERROR = 1
EXIT_SCHEDULE_ERROR = 1
LOGIN_CANCELLED_MESSAGE = "Login cancelled. Nothing was saved."
POLLING_ENDED_REASON = "polling ended unexpectedly"
STORAGE_FAILED_MESSAGE = "storage failed: {error}"


def _report_collector_exit(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("profile collector stopped", extra={"error": type(error).__name__})


def _report_announcer_exit(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("card announcer stopped", extra={"error": type(error).__name__})


def _report_sender_exit(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("outbox sender stopped", extra={"error": type(error).__name__})


def _watch_polling(
    task: asyncio.Task[None], stop: asyncio.Event, failures: list[ControlBotError]
) -> None:
    if task.cancelled() or stop.is_set():
        return
    error = task.exception()
    if isinstance(error, ControlBotError):
        failure = error
    else:
        failure = PollingFailed(type(error).__name__ if error is not None else POLLING_ENDED_REASON)
    logger.error("control bot polling failed", extra={"error": type(failure).__name__})
    failures.append(failure)
    stop.set()


async def run(
    settings: Settings,
    stop: asyncio.Event | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
    client_factory: Callable[[Settings], UserbotAccess] = build_access,
) -> None:
    ensure_session_exists(settings.userbot_session_path)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info("cyrano started", extra={"service": "app", **settings.safe_summary()})
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    contacts = SqlContactRepository(session_factory)
    messages = SqlMessageRepository(session_factory)
    modes = SqlModeRepository(session_factory)
    new_contacts = InMemoryContactEvents[NewContactEvent]()
    ready_contacts = InMemoryContactEvents[ContactReadyEvent]()
    outbox = SqlOutboxRepository(session_factory)
    intake = IntakeService(contacts, messages, new_contacts)
    control_bot = ControlBot(settings, CardActions(contacts, modes), PauseActions(contacts))
    detector = ManualMessageDetector(contacts, messages, outbox, control_bot.manual_pause_notifier)
    access = client_factory(settings)
    client = access.client
    userbot = Userbot(client, intake, detector)
    collector = ProfileCollector(
        contacts,
        TelethonProfileSource(client),
        TelethonLeomatchSource(client, settings.leomatch_bot_username),
        new_contacts,
        ready_contacts,
        settings.media_dir,
    )
    announcer = CardAnnouncer(contacts, messages, modes, control_bot.sender, ready_contacts)
    sender = OutboxSender(
        outbox,
        contacts,
        SqlSenderStateRepository(session_factory),
        SendLimiter(messages, settings),
        ScheduleService(SqlScheduleProfileRepository(session_factory), settings.timezone),
        access.transport,
        control_bot.notifier,
        SendGuard(settings),
        OUTBOX_NOTICES,
    )
    heartbeat = asyncio.create_task(heartbeat_loop(settings.heartbeat_path, heartbeat_interval))
    collector_task: asyncio.Task[None] | None = None
    announcer_task: asyncio.Task[None] | None = None
    polling_task: asyncio.Task[None] | None = None
    sender_task: asyncio.Task[None] | None = None
    polling_failures: list[ControlBotError] = []
    try:
        await userbot.start(stop)
        if not stop.is_set():
            collector_task = asyncio.create_task(collector.run())
            collector_task.add_done_callback(_report_collector_exit)
            announcer_task = asyncio.create_task(announcer.run())
            announcer_task.add_done_callback(_report_announcer_exit)
            polling_task = asyncio.create_task(control_bot.poll())
            polling_task.add_done_callback(
                lambda task: _watch_polling(task, stop, polling_failures)
            )
            sender_task = asyncio.create_task(sender.run())
            sender_task.add_done_callback(_report_sender_exit)
        await userbot.run_until(stop)
        if polling_failures:
            raise polling_failures[0]
    finally:
        logger.info("cyrano shutting down", extra={"service": "app"})
        if sender_task is not None:
            sender_task.cancel()
            await asyncio.gather(sender_task, return_exceptions=True)
        if announcer_task is not None:
            announcer_task.cancel()
            await asyncio.gather(announcer_task, return_exceptions=True)
        await control_bot.stop()
        if polling_task is not None:
            polling_task.cancel()
            await asyncio.gather(polling_task, return_exceptions=True)
        if collector_task is not None:
            collector_task.cancel()
            await asyncio.gather(collector_task, return_exceptions=True)
        await userbot.stop()
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        settings.heartbeat_path.unlink(missing_ok=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        await engine.dispose()
        logger.info("cyrano stopped", extra={"service": "app"})


async def run_llm_check(client: LLMClient, model: str) -> int:
    try:
        report = await check_llm(client)
    except LLMError as exc:
        print(f"llm-check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_LLM_ERROR
    cache = (
        f" cache_hit_tokens={report.cache_hit_tokens}"
        if report.cache_hit_tokens is not None
        else ""
    )
    print(
        f"llm-check ok: model={model} input_tokens={report.input_tokens} "
        f"output_tokens={report.output_tokens}{cache} latency_ms={report.latency_ms}"
    )
    return 0


async def _run_llm_check_command(settings: Settings) -> int:
    factory = LLMClientFactory(settings)
    try:
        client = factory.get(LLMStep.REPLY)
        return await run_llm_check(client, client.model)
    finally:
        await factory.aclose()


async def _run_style_command(settings: Settings, request: StyleRequest) -> int:
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    personas = SqlPersonaRepository(session_factory)
    examples = SqlStyleExampleRepository(session_factory)
    commands = StyleCommands(
        PersonaImporter(personas),
        ExamplesImporter(examples),
        PersonaService(personas),
        examples,
    )
    try:
        return await commands.run(request)
    except (SQLAlchemyError, OSError) as exc:
        print(STORAGE_FAILED_MESSAGE.format(error=type(exc).__name__), file=sys.stderr)
        return EXIT_STYLE_ERROR
    finally:
        await engine.dispose()


async def _run_reply_preview_command(settings: Settings, request: ReplyPreviewRequest) -> int:
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    personas = SqlPersonaRepository(session_factory)
    factory = LLMClientFactory(settings)
    try:
        client = factory.get(LLMStep.REPLY)
        generator = ReplyGenerator(
            contacts=SqlContactRepository(session_factory),
            messages=SqlMessageRepository(session_factory),
            modes=SqlModeRepository(session_factory),
            persona=PersonaService(personas),
            examples=ExampleSelector(
                SqlStyleExampleRepository(session_factory), settings.style_examples_max
            ),
            builder=PromptBuilder(
                budget_chars=settings.reply_prompt_budget_chars,
                timezone=settings.tzinfo,
                clock=lambda: datetime.now(UTC),
            ),
            client=client,
            history_limit=settings.reply_history_messages,
        )
        return await ReplyPreviewCommand(generator, client.model).run(request)
    except (SQLAlchemyError, OSError) as exc:
        print(STORAGE_FAILED_MESSAGE.format(error=type(exc).__name__), file=sys.stderr)
        return EXIT_REPLY_PREVIEW_ERROR
    finally:
        await factory.aclose()
        await engine.dispose()


async def _run_outbox_command(settings: Settings, request: OutboxRequest) -> int:
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    outbox = SqlOutboxRepository(session_factory)
    service = OutboxService(SqlContactRepository(session_factory), outbox, SendGuard(settings))
    commands = OutboxCommands(
        service,
        outbox,
        SqlSenderStateRepository(session_factory),
        SendLimiter(SqlMessageRepository(session_factory), settings),
        clock=lambda: datetime.now(UTC),
    )
    try:
        return await commands.run(request)
    except (SQLAlchemyError, OSError) as exc:
        print(STORAGE_FAILED_MESSAGE.format(error=type(exc).__name__), file=sys.stderr)
        return EXIT_OUTBOX_ERROR
    finally:
        await engine.dispose()


async def _run_schedule_command(settings: Settings, request: ScheduleRequest) -> int:
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    profiles = SqlScheduleProfileRepository(session_factory)
    service = ScheduleService(profiles, settings.timezone)
    commands = ScheduleCommands(
        ScheduleImporter(profiles),
        service,
        HumanizationPlanner(SqlMessageRepository(session_factory), service, random.SystemRandom()),
        profiles,
        clock=lambda: datetime.now(UTC),
    )
    try:
        return await commands.run(request)
    except (SQLAlchemyError, OSError) as exc:
        print(STORAGE_FAILED_MESSAGE.format(error=type(exc).__name__), file=sys.stderr)
        return EXIT_SCHEDULE_ERROR
    finally:
        await engine.dispose()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m cyrano", description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser(LOGIN_COMMAND, help="log the second account in and save the session")
    commands.add_parser(LLM_CHECK_COMMAND, help="send one tiny request to the LLM provider")
    add_convert_parser(add_style_parsers(commands))
    add_reply_preview_parser(commands)
    add_outbox_parser(commands)
    add_schedule_parser(commands)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    convert = convert_request(args)
    if convert is not None:
        return run_convert(convert)
    try:
        outbox = outbox_request(args)
    except OutboxUsageError as exc:
        print(exc, file=sys.stderr)
        return EXIT_OUTBOX_ERROR
    try:
        schedule = schedule_request(args)
    except ScheduleUsageError as exc:
        print(exc, file=sys.stderr)
        return EXIT_SCHEDULE_ERROR
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_CONFIG_ERROR
    configure_logging(settings.log_level, settings.library_log_levels)
    if args.command == LLM_CHECK_COMMAND:
        return asyncio.run(_run_llm_check_command(settings))
    if outbox is not None:
        return asyncio.run(_run_outbox_command(settings, outbox))
    if schedule is not None:
        return asyncio.run(_run_schedule_command(settings, schedule))
    preview = reply_preview_request(args)
    if preview is not None:
        return asyncio.run(_run_reply_preview_command(settings, preview))
    request = style_request(args)
    if request is not None:
        return asyncio.run(_run_style_command(settings, request))
    try:
        if args.command == LOGIN_COMMAND:
            asyncio.run(run_login(settings))
        else:
            asyncio.run(run(settings))
    except (UserbotError, ControlBotError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_USERBOT_ERROR
    except EOFError, KeyboardInterrupt:
        if args.command != LOGIN_COMMAND:
            raise
        print(LOGIN_CANCELLED_MESSAGE, file=sys.stderr)
        return EXIT_USERBOT_ERROR
    return 0
