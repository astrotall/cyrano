from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from cyrano.bot.notifier import AUTOPILOT_NOTICES
from cyrano.config import DEFAULT_AUTOPILOT_MAX_BURST_WAIT_SECONDS
from cyrano.db.repositories import (
    SqlAutopilotStateRepository,
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlPersonaRepository,
    SqlReplyBacklogRepository,
    SqlStyleExampleRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactStatus, MessageDirection
from cyrano.domain.llm import LLMMessage, LLMResult, LLMUsage
from cyrano.domain.models import (
    DEFAULT_MODE_NAME,
    Contact,
    ContactCreate,
    Message,
    MessageCreate,
)
from cyrano.logging import JsonFormatter
from cyrano.services.gender import GenderResolver
from cyrano.services.hand_over import HandOverService
from cyrano.services.humanization import HumanizationPlanner
from cyrano.services.llm_client import LLMClient
from cyrano.services.outbox_service import OutboxService
from cyrano.services.persona_service import PersonaService
from cyrano.services.reply_generator import ReplyGenerator
from cyrano.services.reply_job import ReplyJobRunner
from cyrano.services.send_guard import SendGuard
from cyrano.services.style_examples import ExampleSelector
from tests.reply_data import make_builder
from tests.services.outbox_fakes import (
    FakeClock,
    RecordingHandOverNotifier,
    RecordingNotifier,
    free_schedule,
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
PLANNER_SEED = 17
BASE_MAX_TOKENS = 400
PERSONA_TEXT_ZQ = "persona-autopilot-marker-zq invented and short"
CONTACT_NAME_ZQ = "Invented-Autopilot-Name-zq"
CONTACT_USERNAME_ZQ = "invented_autopilot_handle_zq"
INCOMING_TEXT_ZQ = "incoming-autopilot-marker-zq how was the invented weekend"
REPLY_TEXT_ZQ = "reply-autopilot-marker-zq it was quiet and invented"
NEEDS_OWNER_REPLY = "NEEDS_OWNER"
INVALID_REPLY = "```"
FIRST_TG_ID = 777000111222
FIRST_TG_MESSAGE_ID = 4100


def llm_result(text: str = REPLY_TEXT_ZQ, *, truncated: bool = False) -> LLMResult:
    return LLMResult(
        text=text,
        usage=LLMUsage(input_tokens=120, output_tokens=12, cache_hit_tokens=None),
        truncated=truncated,
    )


class SequenceLLMClient:
    def __init__(self, *steps: LLMResult | Exception) -> None:
        self._steps = list(steps)
        self.max_tokens: list[int | None] = []

    @property
    def calls(self) -> int:
        return len(self.max_tokens)

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.max_tokens.append(max_tokens)
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        if isinstance(step, Exception):
            raise step
        return step

    async def complete_json[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        raise NotImplementedError


@dataclass
class Switch:
    autopilot_enabled: bool = True


@dataclass
class AutopilotRig:
    session_factory: SessionFactory
    clock: FakeClock = field(default_factory=lambda: FakeClock(NOW))
    switch: Switch = field(default_factory=Switch)
    guard_switch: Switch = field(default_factory=Switch)
    notifier: RecordingNotifier = field(default_factory=RecordingNotifier)
    hand_over_notifier: RecordingHandOverNotifier = field(default_factory=RecordingHandOverNotifier)
    tg_counter: int = 0

    def __post_init__(self) -> None:
        factory = self.session_factory
        self.contacts = SqlContactRepository(factory)
        self.messages = SqlMessageRepository(factory)
        self.modes = SqlModeRepository(factory)
        self.outbox = SqlOutboxRepository(factory)
        self.personas = SqlPersonaRepository(factory)
        self.examples = SqlStyleExampleRepository(factory)
        self.state = SqlAutopilotStateRepository(factory)
        self.backlog = SqlReplyBacklogRepository(factory)
        self.schedule = free_schedule()

    def generator(self, client: LLMClient, *, budget: int = 1_000_000) -> ReplyGenerator:
        return ReplyGenerator(
            contacts=self.contacts,
            messages=self.messages,
            modes=self.modes,
            persona=PersonaService(self.personas),
            examples=ExampleSelector(self.examples, 8),
            gender=GenderResolver(),
            builder=make_builder(budget),
            client=client,
            history_limit=20,
        )

    def planner(self) -> HumanizationPlanner:
        return HumanizationPlanner(self.messages, self.schedule, random.Random(PLANNER_SEED))

    def hand_over(self) -> HandOverService:
        return HandOverService(self.contacts, self.messages, self.outbox, self.hand_over_notifier)

    def runner(
        self,
        client: LLMClient,
        *,
        budget: int = 1_000_000,
        planner: HumanizationPlanner | None = None,
        max_tokens: int = BASE_MAX_TOKENS,
        max_burst_wait_seconds: int = DEFAULT_AUTOPILOT_MAX_BURST_WAIT_SECONDS,
    ) -> ReplyJobRunner:
        return ReplyJobRunner(
            contacts=self.contacts,
            messages=self.messages,
            outbox=self.outbox,
            state=self.state,
            generator=self.generator(client, budget=budget),
            hand_over=self.hand_over(),
            planner=planner or self.planner(),
            enqueuer=OutboxService(self.contacts, self.outbox, SendGuard(self.guard_switch)),
            notifier=self.notifier,
            notices=AUTOPILOT_NOTICES,
            switch=self.switch,
            max_tokens=max_tokens,
            max_burst_wait_seconds=max_burst_wait_seconds,
            clock=self.clock,
        )

    async def contact(self, status: ContactStatus = ContactStatus.ACTIVE) -> Contact:
        await self.personas.replace(PERSONA_TEXT_ZQ)
        self.tg_counter += 1
        contact = await self.contacts.create(
            ContactCreate(
                tg_id=FIRST_TG_ID + self.tg_counter,
                username=CONTACT_USERNAME_ZQ,
                display_name=CONTACT_NAME_ZQ,
            )
        )
        if status is ContactStatus.NEW:
            return contact
        contact = await self.contacts.set_status(contact.id, ContactStatus.PENDING)
        if status is ContactStatus.PENDING:
            return contact
        if status is ContactStatus.IGNORED:
            return await self.contacts.set_status(contact.id, ContactStatus.IGNORED)
        return await self.activate(contact.id, status)

    async def activate(
        self, contact_id: int, status: ContactStatus = ContactStatus.ACTIVE
    ) -> Contact:
        default = await self.modes.get_by_name(DEFAULT_MODE_NAME)
        assert default is not None
        contact = await self.contacts.set_status(
            contact_id, ContactStatus.ACTIVE, mode_id=default.id
        )
        if status is ContactStatus.PAUSED:
            return await self.contacts.set_status(contact.id, ContactStatus.PAUSED)
        return contact

    async def say(
        self,
        contact: Contact,
        *,
        ago: float = 60,
        text: str = INCOMING_TEXT_ZQ,
        direction: MessageDirection = MessageDirection.INCOMING,
    ) -> Message:
        self.tg_counter += 1
        return await self.messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=direction,
                text=text,
                tg_message_id=FIRST_TG_MESSAGE_ID + self.tg_counter,
                created_at=self.clock() - timedelta(seconds=ago),
            )
        )

    async def reload(self, contact: Contact) -> Contact:
        current = await self.contacts.get_by_id(contact.id)
        assert current is not None
        return current


@pytest.fixture
def rig(session_factory: SessionFactory) -> AutopilotRig:
    return AutopilotRig(session_factory)


def json_logs(caplog: pytest.LogCaptureFixture) -> str:
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


def capture_everything(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
