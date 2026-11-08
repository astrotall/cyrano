from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactStatus,
    EarlyReadReason,
    MessageDirection,
    PauseReason,
)
from cyrano.domain.models import Contact, ContactCreate, IncomingMessage, Message, MessageCreate
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.early_read import (
    EarlyReadGate,
    EarlyReadQueue,
    EarlyReadScheduler,
)
from cyrano.services.hand_over import HandOverService
from cyrano.services.humanization import HumanizationPlanner, ScheduleService
from cyrano.services.intake import IntakeService
from cyrano.services.message_transport import MessageTransport
from cyrano.services.outbox_sender import OutboxSender, SenderTimings
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.privacy import FAKE_TELEGRAM_ID
from tests.services.autopilot_fakes import InMemoryAutopilotState
from tests.services.outbox_fakes import (
    ALWAYS_FREE_PROFILE,
    FREE_TIMEZONE,
    FakeClock,
    FakeLimits,
    InMemoryOutbox,
    InMemoryScheduleProfiles,
    InMemorySenderState,
    RecordingHandOverNotifier,
    RecordingNotifier,
    ScriptedTransport,
)
from tests.userbot.fakes import InMemoryContacts, InMemoryMessages

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
NIGHT_NOW = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
SEED = 5
CONTACT_NAME_ZQ = "Invented-Early-Name-zq"
CONTACT_USERNAME_ZQ = "invented_early_handle_zq"
INCOMING_TEXT_ZQ = "incoming-early-marker-zq how is the invented evening"
MEETING_TEXT = "давай встретимся завтра вечером"
ACTIVE_ACTIVITY_AGO = timedelta(minutes=1)
NIGHT_PROFILE = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": FREE_TIMEZONE,
        "night": {
            "start": ["00:30", "00:30"],
            "end_weekday": ["08:00", "08:00"],
            "end_weekend": ["08:00", "08:00"],
        },
    }
)
CLASS_PROFILE = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": FREE_TIMEZONE,
        "busy": [{"days": ["wed"], "from": "11:00", "to": "13:00"}],
    }
)


def free_profile_with(**fields: object) -> ScheduleProfile:
    return ScheduleProfile.model_validate(
        {**ALWAYS_FREE_PROFILE.model_dump(mode="json", by_alias=True), **fields}
    )


@dataclass
class Switch:
    autopilot_enabled: bool = True
    early_read_enabled: bool = True


type Break = Callable[[EarlyReadRig], Awaitable[None]]


async def _switch_early_read_off(rig: EarlyReadRig) -> None:
    rig.switch.early_read_enabled = False


async def _switch_autopilot_off(rig: EarlyReadRig) -> None:
    rig.switch.autopilot_enabled = False


async def _stop_autopilot(rig: EarlyReadRig) -> None:
    await rig.autopilot.stop(AutopilotStopReason.PERSONA_MISSING)


async def _cool_autopilot_down(rig: EarlyReadRig) -> None:
    await rig.autopilot.pause_until(
        NOW + timedelta(minutes=10), AutopilotPauseReason.PROVIDER_UNAVAILABLE, now=NOW
    )


async def _pause_sender(rig: EarlyReadRig) -> None:
    await rig.sender_state.pause_until(NOW + timedelta(minutes=10), "FakeFloodWaitError", now=NOW)


async def _stop_sender(rig: EarlyReadRig) -> None:
    await rig.sender_state.stop("FakeBannedError")


BREAKS: list[tuple[EarlyReadReason, Break]] = [
    (EarlyReadReason.DISABLED, _switch_early_read_off),
    (EarlyReadReason.AUTOPILOT_OFF, _switch_autopilot_off),
    (EarlyReadReason.AUTOPILOT_STOPPED, _stop_autopilot),
    (EarlyReadReason.AUTOPILOT_PAUSED, _cool_autopilot_down),
    (EarlyReadReason.SENDER_PAUSED, _pause_sender),
    (EarlyReadReason.SENDER_STOPPED, _stop_sender),
]


class EarlyReadRig:
    def __init__(
        self, profile: ScheduleProfile = ALWAYS_FREE_PROFILE, start: datetime = NOW
    ) -> None:
        self.clock = FakeClock(start)
        self.contacts = InMemoryContacts()
        self.messages = InMemoryMessages()
        self.outbox = InMemoryOutbox()
        self.autopilot = InMemoryAutopilotState()
        self.sender_state = InMemorySenderState()
        self.profiles = InMemoryScheduleProfiles(profile)
        self.schedule = ScheduleService(self.profiles, FREE_TIMEZONE)
        self.switch = Switch()
        self.queue = EarlyReadQueue()
        self.notifier = RecordingNotifier()
        self.hand_over_notifier = RecordingHandOverNotifier()
        self.transport = ScriptedTransport()
        self.gate = EarlyReadGate(
            self.contacts,
            self.autopilot,
            self.sender_state,
            self.schedule,
            self.switch,
            clock=self.clock,
        )
        self.planner = HumanizationPlanner(self.messages, self.schedule, random.Random(SEED))
        self.scheduler = EarlyReadScheduler(
            self.gate,
            self.planner,
            self.schedule,
            self.queue,
            rng=random.Random(SEED),
            clock=self.clock,
        )
        self._message_counter = 0

    def sender(
        self,
        *,
        transport: MessageTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        timings: SenderTimings | None = None,
        limits: FakeLimits | None = None,
        with_early_reads: bool = True,
    ) -> OutboxSender:
        async def no_sleep(seconds: float) -> None:
            await asyncio.sleep(0)

        return OutboxSender(
            self.outbox,
            self.contacts,
            self.messages,
            self.sender_state,
            SendLimiter(self.messages, limits if limits is not None else FakeLimits()),
            self.schedule,
            transport if transport is not None else self.transport,
            self.notifier,
            SendGuard(self.switch),
            OUTBOX_NOTICES,
            clock=self.clock,
            timings=timings,
            sleep=sleep if sleep is not None else no_sleep,
            rng=random.Random(SEED),
            early_reads=self.queue if with_early_reads else None,
            early_gate=self.gate if with_early_reads else None,
        )

    def intake(
        self,
        *,
        hand_over: bool = False,
        early_read: EarlyReadScheduler | None = None,
    ) -> IntakeService:
        return IntakeService(
            self.contacts,
            self.messages,
            InMemoryContactEvents(),
            hand_over=(
                HandOverService(self.contacts, self.messages, self.outbox, self.hand_over_notifier)
                if hand_over
                else None
            ),
            early_read=early_read if early_read is not None else self.scheduler,
            clock=self.clock,
        )

    async def contact(self, status: ContactStatus = ContactStatus.ACTIVE) -> Contact:
        contact = await self.contacts.create(
            ContactCreate(
                tg_id=FAKE_TELEGRAM_ID + len(self.contacts.stored),
                username=CONTACT_USERNAME_ZQ,
                display_name=CONTACT_NAME_ZQ,
            )
        )
        if status is ContactStatus.NEW:
            return contact
        await self.contacts.set_status(contact.id, ContactStatus.PENDING)
        if status is ContactStatus.PENDING:
            return await self._current(contact)
        if status is ContactStatus.IGNORED:
            await self.contacts.set_status(contact.id, ContactStatus.IGNORED)
            return await self._current(contact)
        await self.contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=1)
        if status is ContactStatus.PAUSED:
            await self.contacts.set_status(
                contact.id, ContactStatus.PAUSED, pause_reason=PauseReason.OWNER
            )
        return await self._current(contact)

    async def _current(self, contact: Contact) -> Contact:
        current = await self.contacts.get_by_id(contact.id)
        assert current is not None
        return current

    async def say(
        self,
        contact: Contact,
        ago: timedelta = timedelta(),
        direction: MessageDirection = MessageDirection.INCOMING,
    ) -> Message:
        self._message_counter += 1
        return await self.messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=direction,
                text=INCOMING_TEXT_ZQ,
                tg_message_id=self._message_counter,
                created_at=self.clock() - ago,
            )
        )

    async def ongoing_message(self, contact: Contact) -> Message:
        await self.say(contact, ACTIVE_ACTIVITY_AGO, MessageDirection.OUTGOING)
        return await self.say(contact)

    async def fresh_message(self, contact: Contact) -> Message:
        return await self.say(contact)

    def incoming(self, contact: Contact, text: str = INCOMING_TEXT_ZQ) -> IncomingMessage:
        self._message_counter += 1
        return IncomingMessage(
            tg_id=contact.tg_id,
            tg_message_id=1000 + self._message_counter,
            text=text,
            sent_at=self.clock(),
        )
