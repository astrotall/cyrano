from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from cyrano.bot.notifier import AUTOPILOT_NOTICES
from cyrano.db.repositories import SqlProactiveRepository, SqlSenderStateRepository
from cyrano.domain.enums import MessageDirection
from cyrano.domain.models import Contact, Message
from cyrano.services.humanization import ScheduleService
from cyrano.services.llm_client import LLMClient
from cyrano.services.outbox_service import OutboxService
from cyrano.services.proactive_eligibility import ProactiveEligibility
from cyrano.services.proactive_job import ProactiveJobRunner
from cyrano.services.reply_job import GenerationPolicy
from cyrano.services.send_guard import SendGuard
from tests.services.autopilot_rig import BASE_MAX_TOKENS, AutopilotRig
from tests.services.proactive_data import Rules

PROACTIVE_SEED = 43
HOUR_SECONDS = 3600
OUR_TEXT_ZQ = "our-last-message-marker-zq about the invented ride"
FOLLOW_UP_TEXT_ZQ = "follow-up-marker-zq how did the invented ride go"


@dataclass
class ProactiveRig:
    rig: AutopilotRig
    rules: Rules = field(default_factory=Rules)

    def __post_init__(self) -> None:
        factory = self.rig.session_factory
        self.proactive = SqlProactiveRepository(factory)
        self.sender_state = SqlSenderStateRepository(factory)

    def eligibility(self) -> ProactiveEligibility:
        return ProactiveEligibility(
            self.rig.contacts,
            self.rig.state,
            self.sender_state,
            self.proactive,
            self.rules,
            self.rig.schedule,
            clock=self.rig.clock,
        )

    def policy(
        self, client: LLMClient, *, budget: int = 1_000_000, quote_enabled: bool = False
    ) -> GenerationPolicy:
        return GenerationPolicy(
            state=self.rig.state,
            generator=self.rig.generator(client, budget=budget, quote_enabled=quote_enabled),
            hand_over=self.rig.hand_over(),
            notifier=self.rig.notifier,
            notices=AUTOPILOT_NOTICES,
            max_tokens=BASE_MAX_TOKENS,
            clock=self.rig.clock,
        )

    def runner(
        self,
        client: LLMClient,
        *,
        budget: int = 1_000_000,
        schedule: ScheduleService | None = None,
        seed: int = PROACTIVE_SEED,
        quote_enabled: bool = False,
    ) -> ProactiveJobRunner:
        return ProactiveJobRunner(
            eligibility=self.eligibility(),
            proactive=self.proactive,
            outbox=self.rig.outbox,
            policy=self.policy(client, budget=budget, quote_enabled=quote_enabled),
            schedule=schedule if schedule is not None else self.rig.schedule,
            enqueuer=OutboxService(
                self.rig.contacts, self.rig.outbox, SendGuard(self.rig.guard_switch)
            ),
            rng=random.Random(seed),
            clock=self.rig.clock,
        )

    async def silent_contact(self, hours: float = 7) -> tuple[Contact, Message]:
        contact = await self.rig.contact()
        await self.rig.say(contact, ago=(hours + 1) * HOUR_SECONDS)
        last = await self.rig.say(
            contact,
            ago=hours * HOUR_SECONDS,
            text=OUR_TEXT_ZQ,
            direction=MessageDirection.OUTGOING,
        )
        return contact, last

    def later(self, hours: float) -> None:
        self.rig.clock.advance(timedelta(hours=hours).total_seconds())


@pytest.fixture
def prig(rig: AutopilotRig) -> ProactiveRig:
    return ProactiveRig(rig)
