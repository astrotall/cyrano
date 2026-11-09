from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from cyrano.bot.app import NoticeBot
from cyrano.bot.notifier import AUTOPILOT_NOTICES
from cyrano.config import Settings
from cyrano.db.repositories import (
    SqlAutopilotStateRepository,
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlPersonaRepository,
    SqlProactiveRepository,
    SqlScheduleProfileRepository,
    SqlSenderStateRepository,
    SqlStyleExampleRepository,
    SqlSummaryRepository,
)
from cyrano.db.session import build_engine, build_session_factory
from cyrano.domain.enums import LLMStep
from cyrano.llm.factory import LLMClientFactory
from cyrano.services.gender import GenderResolver
from cyrano.services.hand_over import HandOverService
from cyrano.services.humanization import HumanizationPlanner, ScheduleService
from cyrano.services.outbox_service import OutboxService
from cyrano.services.persona_service import PersonaService
from cyrano.services.proactive_eligibility import ProactiveEligibility
from cyrano.services.proactive_job import ProactiveJobRunner
from cyrano.services.prompt_builder import PromptBuilder
from cyrano.services.reply_generator import ReplyGenerator
from cyrano.services.reply_job import GenerationPolicy, ReplyJobRunner
from cyrano.services.send_guard import SendGuard
from cyrano.services.style_examples import ExampleSelector
from cyrano.services.summary_updater import SummaryUpdater


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class WorkerResources:
    runner: ReplyJobRunner
    summary_updater: SummaryUpdater
    proactive_runner: ProactiveJobRunner
    engine: AsyncEngine
    llm: LLMClientFactory
    bot: NoticeBot

    async def aclose(self) -> None:
        try:
            await self.bot.close()
        finally:
            try:
                await self.llm.aclose()
            finally:
                await self.engine.dispose()


def build_worker_resources(
    settings: Settings, *, notice_bot: Callable[[Settings], NoticeBot] = NoticeBot
) -> WorkerResources:
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    contacts = SqlContactRepository(session_factory)
    messages = SqlMessageRepository(session_factory)
    outbox = SqlOutboxRepository(session_factory)
    llm = LLMClientFactory(settings)
    bot = notice_bot(settings)
    generator = ReplyGenerator(
        contacts=contacts,
        messages=messages,
        modes=SqlModeRepository(session_factory),
        persona=PersonaService(SqlPersonaRepository(session_factory)),
        examples=ExampleSelector(
            SqlStyleExampleRepository(session_factory), settings.style_examples_max
        ),
        gender=GenderResolver(settings.default_gender),
        builder=PromptBuilder(
            budget_chars=settings.reply_prompt_budget_chars,
            timezone=settings.tzinfo,
            clock=_utc_now,
            quote_enabled=settings.reply_quote_enabled,
        ),
        client=llm.get(LLMStep.REPLY),
        history_limit=settings.reply_history_messages,
    )
    schedule = ScheduleService(SqlScheduleProfileRepository(session_factory), settings.timezone)
    state = SqlAutopilotStateRepository(session_factory)
    hand_over = HandOverService(contacts, messages, outbox, bot.hand_over_notifier)
    enqueuer = OutboxService(contacts, outbox, SendGuard(settings))
    runner = ReplyJobRunner(
        contacts=contacts,
        messages=messages,
        outbox=outbox,
        state=state,
        generator=generator,
        hand_over=hand_over,
        planner=HumanizationPlanner(messages, schedule, random.SystemRandom()),
        enqueuer=enqueuer,
        notifier=bot.notifier,
        notices=AUTOPILOT_NOTICES,
        switch=settings,
        max_tokens=settings.llm_reply_max_tokens,
        max_burst_wait_seconds=settings.autopilot_max_burst_wait_seconds,
        clock=_utc_now,
        wrapup_enabled=settings.wrapup_enabled,
        quote_enabled=settings.reply_quote_enabled,
    )
    proactive = SqlProactiveRepository(session_factory)
    proactive_runner = ProactiveJobRunner(
        eligibility=ProactiveEligibility(
            contacts,
            state,
            SqlSenderStateRepository(session_factory),
            proactive,
            settings,
            schedule,
            clock=_utc_now,
        ),
        proactive=proactive,
        outbox=outbox,
        policy=GenerationPolicy(
            state=state,
            generator=generator,
            hand_over=hand_over,
            notifier=bot.notifier,
            notices=AUTOPILOT_NOTICES,
            max_tokens=settings.llm_reply_max_tokens,
            clock=_utc_now,
        ),
        schedule=schedule,
        enqueuer=enqueuer,
        rng=random.SystemRandom(),
        clock=_utc_now,
    )
    summary_updater = SummaryUpdater(
        contacts=contacts,
        summaries=SqlSummaryRepository(session_factory),
        client=llm.get(LLMStep.SUMMARY),
        clock=_utc_now,
    )
    return WorkerResources(
        runner=runner,
        summary_updater=summary_updater,
        proactive_runner=proactive_runner,
        engine=engine,
        llm=llm,
        bot=bot,
    )
