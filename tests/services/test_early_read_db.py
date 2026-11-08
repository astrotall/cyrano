from __future__ import annotations

import random
from datetime import timedelta

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import (
    SqlAutopilotStateRepository,
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlScheduleProfileRepository,
    SqlSenderStateRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactStatus, EarlyReadOutcome, MessageDirection
from cyrano.domain.errors import SendFloodWait
from cyrano.domain.models import IncomingMessage, MessageCreate
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.early_read import (
    EarlyReadGate,
    EarlyReadQueue,
    EarlyReadScheduler,
)
from cyrano.services.humanization import HumanizationPlanner, ScheduleService
from cyrano.services.intake import IntakeService
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.privacy import FAKE_TELEGRAM_ID
from tests.services.early_read_rig import NOW, SEED, Switch, free_profile_with
from tests.services.outbox_fakes import (
    FREE_TIMEZONE,
    FakeClock,
    FakeLimits,
    RecordingNotifier,
    ScriptedTransport,
    stored_contact,
)

FLOOD_SECONDS = 90


class _Setup:
    def __init__(
        self,
        session_factory: SessionFactory,
        contacts: SqlContactRepository,
        messages: SqlMessageRepository,
        modes: SqlModeRepository,
        outbox: SqlOutboxRepository,
        sender_state: SqlSenderStateRepository,
        schedule_profiles: SqlScheduleProfileRepository,
        transport: ScriptedTransport,
    ) -> None:
        self.contacts = contacts
        self.messages = messages
        self.modes = modes
        self.outbox = outbox
        self.sender_state = sender_state
        self.schedule_profiles = schedule_profiles
        self.transport = transport
        self.clock = FakeClock(NOW)
        self.switch = Switch()
        self.notifier = RecordingNotifier()
        self.queue = EarlyReadQueue()
        schedule = ScheduleService(schedule_profiles, FREE_TIMEZONE)
        self.autopilot_state = SqlAutopilotStateRepository(session_factory)
        self.gate = EarlyReadGate(
            contacts, self.autopilot_state, sender_state, schedule, self.switch, clock=self.clock
        )
        self.scheduler = EarlyReadScheduler(
            self.gate,
            HumanizationPlanner(messages, schedule, random.Random(SEED)),
            schedule,
            self.queue,
            rng=random.Random(SEED),
            clock=self.clock,
        )
        self.sender = OutboxSender(
            outbox,
            contacts,
            messages,
            sender_state,
            SendLimiter(messages, FakeLimits()),
            schedule,
            transport,
            self.notifier,
            SendGuard(self.switch),
            OUTBOX_NOTICES,
            clock=self.clock,
            rng=random.Random(SEED),
            early_reads=self.queue,
            early_gate=self.gate,
        )

    async def lively_contact(self) -> int:
        contact = await stored_contact(
            self.contacts, self.modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID
        )
        await self.messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=MessageDirection.OUTGOING,
                text="an invented earlier line",
                tg_message_id=1,
                created_at=NOW - timedelta(minutes=1),
            )
        )
        return contact.id

    def intake(self) -> IntakeService:
        return IntakeService(
            self.contacts,
            self.messages,
            InMemoryContactEvents(),
            early_read=self.scheduler,
            clock=self.clock,
        )


async def _setup(
    session_factory: SessionFactory,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    sender_state: SqlSenderStateRepository,
    schedule_profiles: SqlScheduleProfileRepository,
    transport: ScriptedTransport | None = None,
) -> _Setup:
    await schedule_profiles.replace(free_profile_with(early_read_seconds=[0, 0]))
    return _Setup(
        session_factory,
        contacts,
        messages,
        modes,
        outbox,
        sender_state,
        schedule_profiles,
        transport if transport is not None else ScriptedTransport(),
    )


async def test_a_lively_message_through_the_intake_is_marked_read_with_real_storage(
    session_factory: SessionFactory,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    sender_state: SqlSenderStateRepository,
    schedule_profiles: SqlScheduleProfileRepository,
) -> None:
    setup = await _setup(
        session_factory, contacts, messages, modes, outbox, sender_state, schedule_profiles
    )
    contact_id = await setup.lively_contact()
    await setup.intake().handle(
        IncomingMessage(
            tg_id=FAKE_TELEGRAM_ID, tg_message_id=2, text="an invented line", sent_at=NOW
        )
    )

    assert [request.contact_id for request in setup.queue.pending()] == [contact_id]
    assert await setup.sender.execute_early_reads() == 1
    assert setup.transport.read_ids == [FAKE_TELEGRAM_ID]
    assert await outbox.list_rows(None, 10) == []


async def test_a_paused_contact_is_skipped_with_real_storage(
    session_factory: SessionFactory,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    sender_state: SqlSenderStateRepository,
    schedule_profiles: SqlScheduleProfileRepository,
) -> None:
    setup = await _setup(
        session_factory, contacts, messages, modes, outbox, sender_state, schedule_profiles
    )
    contact_id = await setup.lively_contact()
    setup.queue.register(contact_id, NOW)
    await contacts.set_status(contact_id, ContactStatus.PAUSED)

    assert await setup.sender.execute_early_reads() == 1

    assert setup.transport.read_ids == []
    assert setup.sender.early_read_counts[EarlyReadOutcome.SKIPPED] == 1


async def test_the_stored_profile_range_decides_the_due_time(
    session_factory: SessionFactory,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    sender_state: SqlSenderStateRepository,
    schedule_profiles: SqlScheduleProfileRepository,
) -> None:
    setup = await _setup(
        session_factory, contacts, messages, modes, outbox, sender_state, schedule_profiles
    )
    await schedule_profiles.replace(free_profile_with(early_read_seconds=[4, 4]))
    contact_id = await setup.lively_contact()
    await messages.append(
        MessageCreate(
            contact_id=contact_id,
            direction=MessageDirection.INCOMING,
            text="an invented line",
            tg_message_id=2,
            created_at=NOW,
        )
    )
    incoming = (await messages.list_last(contact_id, 1))[0]

    assert await setup.scheduler.on_incoming(contact_id, incoming.id) is True

    [request] = setup.queue.pending()
    assert request.due_at == NOW + timedelta(seconds=4)


async def test_a_flood_wait_is_stored_as_a_sender_pause_with_real_storage(
    session_factory: SessionFactory,
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    sender_state: SqlSenderStateRepository,
    schedule_profiles: SqlScheduleProfileRepository,
) -> None:
    transport = ScriptedTransport(read_errors=[SendFloodWait("FakeFloodWaitError", FLOOD_SECONDS)])
    setup = await _setup(
        session_factory,
        contacts,
        messages,
        modes,
        outbox,
        sender_state,
        schedule_profiles,
        transport,
    )
    contact_id = await setup.lively_contact()
    setup.queue.register(contact_id, NOW)

    await setup.sender.execute_early_reads()

    state = await sender_state.get()
    assert state.is_paused(NOW)
    assert state.pause_reason == "FakeFloodWaitError"
    assert len(setup.notifier.texts) == 1
    assert await outbox.list_rows(None, 10) == []
