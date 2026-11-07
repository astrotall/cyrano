from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import update

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.models import ContactRow
from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlSenderStateRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import (
    ContactStatus,
    IgnoreOutcome,
    OutboxSource,
    OutboxState,
    PauseReason,
    ResumeOutcome,
)
from cyrano.domain.errors import InvalidStatusTransition
from cyrano.domain.models import Contact, StatusChange
from cyrano.domain.outbox import REFUSED_CONTACT_NOT_ACTIVE
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.pause_actions import PauseActions
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.privacy import FAKE_TELEGRAM_ID
from tests.services.outbox_fakes import (
    FakeClock,
    FakeLimits,
    RecordingNotifier,
    ScriptedTransport,
    free_schedule,
    stored_contact,
)

MISSING_ID = 999_999
QUEUED_TEXT = "resume-text-marker-zq"


class _Switch:
    autopilot_enabled = False


@pytest.fixture
def actions(contacts: SqlContactRepository) -> PauseActions:
    return PauseActions(contacts)


async def _contact(
    contacts: SqlContactRepository, modes: SqlModeRepository, status: ContactStatus
) -> Contact:
    created = await stored_contact(contacts, modes, status, tg_id=FAKE_TELEGRAM_ID)
    found = await contacts.get_by_id(created.id)
    assert found is not None
    return found


async def _paused_by_manual_message(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> Contact:
    contact = await _contact(contacts, modes, ContactStatus.ACTIVE)
    await contacts.set_status(
        contact.id, ContactStatus.PAUSED, pause_reason=PauseReason.MANUAL_MESSAGE
    )
    found = await contacts.get_by_id(contact.id)
    assert found is not None
    return found


async def test_resume_activates_the_contact_keeps_the_mode_and_clears_the_reason(
    actions: PauseActions, contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_manual_message(contacts, modes)

    outcome = await actions.resume(paused.id)

    assert outcome is ResumeOutcome.APPLIED
    resumed = await contacts.get_by_id(paused.id)
    assert resumed is not None
    assert (resumed.status, resumed.mode_id, resumed.pause_reason) == (
        ContactStatus.ACTIVE,
        paused.mode_id,
        None,
    )


async def test_a_second_click_is_already_applied_and_changes_nothing(
    actions: PauseActions, contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_manual_message(contacts, modes)
    await actions.resume(paused.id)
    after_first = await contacts.get_by_id(paused.id)

    outcome = await actions.resume(paused.id)

    assert outcome is ResumeOutcome.ALREADY_APPLIED
    assert await contacts.get_by_id(paused.id) == after_first


async def test_a_contact_paused_by_a_rejection_can_be_resumed_too(
    actions: PauseActions, contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    contact = await _contact(contacts, modes, ContactStatus.ACTIVE)
    await contacts.set_status(
        contact.id, ContactStatus.PAUSED, pause_reason=PauseReason.SEND_REJECTED
    )

    assert await actions.resume(contact.id) is ResumeOutcome.APPLIED


@pytest.mark.parametrize(
    "status", [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.IGNORED]
)
async def test_resume_is_refused_for_a_contact_that_was_never_paused_or_is_ignored(
    status: ContactStatus,
    actions: PauseActions,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
) -> None:
    contact = await _contact(contacts, modes, status)

    outcome = await actions.resume(contact.id)

    assert outcome is ResumeOutcome.REFUSED
    assert await contacts.get_by_id(contact.id) == contact


async def test_resume_of_a_missing_contact_is_not_found(actions: PauseActions) -> None:
    assert await actions.resume(MISSING_ID) is ResumeOutcome.NOT_FOUND


async def test_a_paused_contact_without_a_mode_is_refused_and_stays_paused(
    actions: PauseActions,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    session_factory: SessionFactory,
) -> None:
    paused = await _paused_by_manual_message(contacts, modes)
    async with session_factory.begin() as session:
        await session.execute(
            update(ContactRow).where(ContactRow.id == paused.id).values(mode_id=None)
        )
    without_mode = await contacts.get_by_id(paused.id)

    outcome = await actions.resume(paused.id)

    assert outcome is ResumeOutcome.REFUSED
    assert await contacts.get_by_id(paused.id) == without_mode


async def test_a_refused_transition_during_the_resume_is_refused(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_manual_message(contacts, modes)

    class _Raced(SqlContactRepository):
        async def set_status(
            self,
            contact_id: int,
            status: ContactStatus,
            *,
            mode_id: int | None = None,
            pause_reason: PauseReason | None = None,
            reply_after: datetime | None = None,
        ) -> StatusChange:
            raise InvalidStatusTransition(ContactStatus.IGNORED, status)

    raced = _Raced(contacts._session_factory)

    assert await PauseActions(raced).resume(paused.id) is ResumeOutcome.REFUSED


async def test_simultaneous_clicks_apply_once(
    actions: PauseActions, contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_manual_message(contacts, modes)

    outcomes = await asyncio.gather(*(actions.resume(paused.id) for _ in range(5)))

    assert sorted(outcomes) == [ResumeOutcome.ALREADY_APPLIED] * 4 + [ResumeOutcome.APPLIED]


async def test_after_a_resume_the_sender_guard_allows_sending_again(
    actions: PauseActions,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    outbox: SqlOutboxRepository,
    sender_state: SqlSenderStateRepository,
) -> None:
    paused = await _paused_by_manual_message(contacts, modes)
    clock = FakeClock()
    transport = ScriptedTransport(clock=clock)
    sender = OutboxSender(
        outbox,
        contacts,
        messages,
        sender_state,
        SendLimiter(messages, FakeLimits()),
        free_schedule(),
        transport,
        RecordingNotifier(),
        SendGuard(_Switch()),
        OUTBOX_NOTICES,
        clock=clock,
    )
    blocked = await outbox.enqueue(paused.id, QUEUED_TEXT, clock(), OutboxSource.MANUAL_TEST)

    await sender.send_next()

    cancelled = await outbox.get(blocked.id)
    assert cancelled is not None
    assert (cancelled.state, cancelled.cancel_reason) == (
        OutboxState.CANCELLED,
        REFUSED_CONTACT_NOT_ACTIVE,
    )
    assert transport.calls == []

    assert await actions.resume(paused.id) is ResumeOutcome.APPLIED
    allowed = await outbox.enqueue(
        paused.id, QUEUED_TEXT, clock() + timedelta(seconds=1), OutboxSource.MANUAL_TEST
    )
    clock.advance(2)
    await sender.send_next()

    sent = await outbox.get(allowed.id)
    assert sent is not None
    assert sent.state is OutboxState.SENT
    assert transport.calls == [(paused.tg_id, QUEUED_TEXT)]


async def _paused_by_stop_trigger(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> Contact:
    contact = await _contact(contacts, modes, ContactStatus.ACTIVE)
    await contacts.set_status(
        contact.id, ContactStatus.PAUSED, pause_reason=PauseReason.STOP_TRIGGER
    )
    found = await contacts.get_by_id(contact.id)
    assert found is not None
    return found


async def test_resume_after_a_stop_trigger_activates_and_clears_the_reason(
    actions: PauseActions, contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_stop_trigger(contacts, modes)

    outcome = await actions.resume(paused.id)

    assert outcome is ResumeOutcome.APPLIED
    resumed = await contacts.get_by_id(paused.id)
    assert resumed is not None
    assert (resumed.status, resumed.mode_id, resumed.pause_reason) == (
        ContactStatus.ACTIVE,
        paused.mode_id,
        None,
    )


@pytest.mark.parametrize(
    "reason", [PauseReason.STOP_TRIGGER, PauseReason.MANUAL_MESSAGE, PauseReason.SEND_REJECTED]
)
async def test_ignore_moves_a_paused_contact_to_ignored_and_clears_the_reason(
    actions: PauseActions,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    reason: PauseReason,
) -> None:
    contact = await _contact(contacts, modes, ContactStatus.ACTIVE)
    await contacts.set_status(contact.id, ContactStatus.PAUSED, pause_reason=reason)

    outcome = await actions.ignore(contact.id)

    assert outcome is IgnoreOutcome.APPLIED
    ignored = await contacts.get_by_id(contact.id)
    assert ignored is not None
    assert (ignored.status, ignored.pause_reason) == (ContactStatus.IGNORED, None)


async def test_a_second_ignore_is_already_applied_and_changes_nothing(
    actions: PauseActions, contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_stop_trigger(contacts, modes)
    await actions.ignore(paused.id)
    before = await contacts.get_by_id(paused.id)

    outcome = await actions.ignore(paused.id)

    assert outcome is IgnoreOutcome.ALREADY_APPLIED
    assert await contacts.get_by_id(paused.id) == before


@pytest.mark.parametrize("status", [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.ACTIVE])
async def test_ignore_is_refused_for_a_contact_that_is_not_paused(
    actions: PauseActions,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    status: ContactStatus,
) -> None:
    contact = await _contact(contacts, modes, status)

    outcome = await actions.ignore(contact.id)

    assert outcome is IgnoreOutcome.REFUSED
    assert await contacts.get_by_id(contact.id) == contact


async def test_ignore_of_a_missing_contact_is_not_found(actions: PauseActions) -> None:
    assert await actions.ignore(MISSING_ID) is IgnoreOutcome.NOT_FOUND


async def test_a_refused_transition_during_the_ignore_is_refused(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_stop_trigger(contacts, modes)

    class _Raced(SqlContactRepository):
        async def set_status(
            self,
            contact_id: int,
            status: ContactStatus,
            *,
            mode_id: int | None = None,
            pause_reason: PauseReason | None = None,
            reply_after: datetime | None = None,
        ) -> StatusChange:
            raise InvalidStatusTransition(ContactStatus.IGNORED, status)

    outcome = await PauseActions(_Raced(contacts._session_factory)).ignore(paused.id)

    assert outcome is IgnoreOutcome.REFUSED


async def test_simultaneous_ignore_clicks_apply_once(
    actions: PauseActions, contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_stop_trigger(contacts, modes)

    outcomes = await asyncio.gather(*(actions.ignore(paused.id) for _ in range(5)))

    assert sorted(outcomes) == sorted([IgnoreOutcome.APPLIED] + [IgnoreOutcome.ALREADY_APPLIED] * 4)


async def test_an_ignored_contact_cannot_be_resumed(
    actions: PauseActions, contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_stop_trigger(contacts, modes)
    await actions.ignore(paused.id)

    assert await actions.resume(paused.id) is ResumeOutcome.REFUSED


async def test_resume_stores_the_resume_time_as_reply_after(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_manual_message(contacts, modes)
    clock = FakeClock()

    outcome = await PauseActions(contacts, clock=clock).resume(paused.id)

    current = await contacts.get_by_id(paused.id)
    assert outcome is ResumeOutcome.APPLIED
    assert current is not None
    assert (current.status, current.reply_after) == (ContactStatus.ACTIVE, clock())


async def test_a_second_resume_keeps_the_first_reply_after(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_manual_message(contacts, modes)
    clock = FakeClock()
    actions = PauseActions(contacts, clock=clock)
    await actions.resume(paused.id)
    first = clock()
    clock.advance(30)

    assert await actions.resume(paused.id) is ResumeOutcome.ALREADY_APPLIED

    current = await contacts.get_by_id(paused.id)
    assert current is not None
    assert current.reply_after == first


async def test_resume_always_passes_a_reply_after(
    contacts: SqlContactRepository, modes: SqlModeRepository
) -> None:
    paused = await _paused_by_manual_message(contacts, modes)
    seen: list[datetime | None] = []

    class _Recording(SqlContactRepository):
        async def set_status(
            self,
            contact_id: int,
            status: ContactStatus,
            *,
            mode_id: int | None = None,
            pause_reason: PauseReason | None = None,
            reply_after: datetime | None = None,
        ) -> StatusChange:
            seen.append(reply_after)
            return await super().set_status(
                contact_id,
                status,
                mode_id=mode_id,
                pause_reason=pause_reason,
                reply_after=reply_after,
            )

    await PauseActions(_Recording(contacts._session_factory)).resume(paused.id)

    assert len(seen) == 1
    assert seen[0] is not None
