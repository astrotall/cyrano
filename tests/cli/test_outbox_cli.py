from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlSenderStateRepository,
)
from cyrano.domain.enums import ContactStatus, OutboxSource, OutboxState
from cyrano.domain.errors import SendUncertain
from cyrano.domain.models import Contact
from cyrano.domain.outbox import (
    CANCEL_REASON_OWNER,
    CANCEL_REASON_STALE,
    ERROR_RATE_LIMITED,
    ERROR_UNCERTAIN,
    MAX_OUTBOX_TEXT_LENGTH,
    REFUSED_CONTACT_NOT_ACTIVE,
    REFUSED_TEXT_EMPTY,
    REFUSED_TEXT_TOO_LONG,
    REFUSED_UNKNOWN_CONTACT,
    ROW_STALE_AFTER_SECONDS,
    ScheduledMessage,
)
from cyrano.main import main
from cyrano.outbox_cli import (
    DEFAULT_IN_SECONDS,
    MAX_IN_SECONDS,
    USAGE,
    OutboxCommands,
    OutboxRequest,
)
from cyrano.services.outbox_sender import OutboxSender
from cyrano.services.outbox_service import OutboxService
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import SendLimiter
from tests.db.conftest import execute, fetch_all
from tests.privacy import FAKE_INTERNAL_ID, FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import (
    FAKE_NOW,
    FakeClock,
    FakeLimits,
    RecordingNotifier,
    ScriptedTransport,
    free_schedule,
    stored_contact,
)

TEXT = "cli-text-marker-zq must never be printed"
LIMITS = FakeLimits(
    send_limit_minute=4,
    send_limit_hour=40,
    send_limit_day=200,
    send_limit_contact_minute=2,
    send_limit_contact_hour=15,
    send_limit_contact_day=60,
)


@dataclass(frozen=True)
class _Switch:
    autopilot_enabled: bool


@pytest.fixture
def commands(
    contacts: SqlContactRepository,
    outbox: SqlOutboxRepository,
    messages: SqlMessageRepository,
    sender_state: SqlSenderStateRepository,
) -> OutboxCommands:
    service = OutboxService(contacts, outbox, SendGuard(_Switch(False)))
    return OutboxCommands(
        service, outbox, sender_state, SendLimiter(messages, LIMITS), clock=FakeClock()
    )


def _enqueue(
    contact_id: int, text: str = TEXT, in_seconds: int = DEFAULT_IN_SECONDS
) -> OutboxRequest:
    return OutboxRequest(action="enqueue", contact_id=contact_id, text=text, in_seconds=in_seconds)


async def test_enqueue_prints_the_row_id_and_the_time_only(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    code = await commands.run(_enqueue(contact.id, in_seconds=30))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    rows = await outbox.list_rows(None, 10)
    assert len(rows) == 1
    assert rows[0].source is OutboxSource.MANUAL_TEST
    assert rows[0].send_at == FAKE_NOW + timedelta(seconds=30)
    assert captured.out == (f"outbox row {rows[0].id} planned for {rows[0].send_at.isoformat()}\n")
    assert_absent(TEXT, captured.out)


async def test_the_default_delay_is_five_seconds(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    await commands.run(_enqueue(contact.id))

    rows = await outbox.list_rows(None, 10)
    assert rows[0].send_at == FAKE_NOW + timedelta(seconds=5)


@pytest.mark.parametrize("seconds", [0, MAX_IN_SECONDS])
async def test_the_delay_boundaries_are_accepted(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    seconds: int,
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    assert await commands.run(_enqueue(contact.id, in_seconds=seconds)) == 0


@pytest.mark.parametrize("seconds", [-1, MAX_IN_SECONDS + 1])
async def test_a_delay_out_of_range_is_refused(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
    seconds: int,
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    code = await commands.run(_enqueue(contact.id, in_seconds=seconds))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "--in-seconds" in captured.err
    assert len(captured.err.splitlines()) == 1
    assert await outbox.list_rows(None, 10) == []


@pytest.mark.parametrize(
    "status",
    [ContactStatus.NEW, ContactStatus.PENDING, ContactStatus.PAUSED, ContactStatus.IGNORED],
)
async def test_enqueue_refuses_a_contact_that_is_not_active(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
    status: ContactStatus,
) -> None:
    contact = await stored_contact(contacts, modes, status, tg_id=FAKE_TELEGRAM_ID)

    code = await commands.run(_enqueue(contact.id))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"outbox enqueue refused: {REFUSED_CONTACT_NOT_ACTIVE}\n"
    assert await outbox.list_rows(None, 10) == []


async def test_enqueue_refuses_an_unknown_contact(
    commands: OutboxCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(_enqueue(FAKE_INTERNAL_ID))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == f"outbox enqueue refused: {REFUSED_UNKNOWN_CONTACT}\n"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", REFUSED_TEXT_EMPTY),
        ("   ", REFUSED_TEXT_EMPTY),
        ("x" * (MAX_OUTBOX_TEXT_LENGTH + 1), REFUSED_TEXT_TOO_LONG),
    ],
)
async def test_enqueue_refuses_a_bad_text_without_printing_it(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    capsys: pytest.CaptureFixture[str],
    text: str,
    reason: str,
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    code = await commands.run(_enqueue(contact.id, text=text))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == f"outbox enqueue refused: {reason}\n"
    assert captured.out == ""


async def test_list_prints_the_fields_and_never_the_text(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)
    row = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    await outbox.mark_failed(claimed.id, "FakeError")

    code = await commands.run(OutboxRequest(action="list"))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == (
        f"id={row.id} contact={contact.id} source=manual_test state=failed "
        f"send_at={FAKE_NOW.isoformat()} due_at={FAKE_NOW.isoformat()} "
        "error=FakeError cancel_reason=-\n"
    )
    assert_absent(TEXT, captured.out + captured.err)
    assert_absent(FAKE_TELEGRAM_ID, captured.out + captured.err)


async def test_list_shows_the_due_time_next_to_a_moved_send_time(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)
    row = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    moved_to = FAKE_NOW + timedelta(hours=1)
    assert await outbox.claim_due(FAKE_NOW) is not None
    await outbox.reschedule(row.id, moved_to, ERROR_RATE_LIMITED)

    assert await commands.run(OutboxRequest(action="list")) == 0

    captured = capsys.readouterr()
    assert (
        f"state=scheduled send_at={moved_to.isoformat()} due_at={FAKE_NOW.isoformat()} "
        f"error={ERROR_RATE_LIMITED} cancel_reason=-\n"
    ) in captured.out
    assert_absent(TEXT, captured.out + captured.err)


async def test_list_tells_every_cancel_reason_apart_and_never_prints_a_text(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)
    by_owner = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    stale = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    refused = await outbox.enqueue(
        contact.id,
        TEXT,
        FAKE_NOW + timedelta(seconds=ROW_STALE_AFTER_SECONDS),
        OutboxSource.MANUAL_TEST,
    )
    assert await commands.run(OutboxRequest(action="cancel", row_id=by_owner.id)) == 0
    clock = FakeClock(FAKE_NOW + timedelta(seconds=ROW_STALE_AFTER_SECONDS + 1))
    sender = _sender(outbox, contacts, ScriptedTransport(), clock)
    await sender.send_next()
    await contacts.set_status(contact.id, ContactStatus.PAUSED)
    await sender.send_next()
    capsys.readouterr()

    assert await commands.run(OutboxRequest(action="list")) == 0

    captured = capsys.readouterr()
    lines = {int(line.split()[0].removeprefix("id=")): line for line in captured.out.splitlines()}
    assert lines[by_owner.id].endswith(f"cancel_reason={CANCEL_REASON_OWNER}")
    assert lines[stale.id].endswith(f"cancel_reason={CANCEL_REASON_STALE}")
    assert lines[refused.id].endswith(f"cancel_reason={REFUSED_CONTACT_NOT_ACTIVE}")
    assert_absent(TEXT, captured.out + captured.err)
    assert_absent(FAKE_TELEGRAM_ID, captured.out + captured.err)


async def test_list_filters_by_state_and_limits(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)
    first = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    third = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    await outbox.mark_cancelled(first.id, "fake_reason")

    await commands.run(OutboxRequest(action="list", state=OutboxState.CANCELLED))
    assert f"id={first.id} " in capsys.readouterr().out

    await commands.run(OutboxRequest(action="list", limit=1))
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert f"id={third.id} " in out


async def test_list_refuses_a_limit_out_of_range(
    commands: OutboxCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(OutboxRequest(action="list", limit=0))

    assert code == 1
    assert "--limit" in capsys.readouterr().err


async def test_cancel_cancels_a_scheduled_row(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)
    row = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)

    code = await commands.run(OutboxRequest(action="cancel", row_id=row.id))

    stored = await outbox.get(row.id)
    assert stored is not None
    assert code == 0
    assert (stored.state, stored.cancel_reason) == (OutboxState.CANCELLED, CANCEL_REASON_OWNER)
    assert capsys.readouterr().out == f"outbox row {row.id} cancelled\n"


@pytest.mark.parametrize("finished", ["sending", "sent", "failed", "cancelled"])
async def test_cancel_refuses_a_row_that_is_no_longer_scheduled(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
    finished: str,
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)
    row = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    if finished == "cancelled":
        await outbox.mark_cancelled(row.id, "fake_reason")
    else:
        await outbox.claim_due(FAKE_NOW)
    if finished == "sent":
        await outbox.complete_send(row.id, contact.id, TEXT, 700001, FAKE_NOW)
    if finished == "failed":
        await outbox.mark_failed(row.id, "FakeError")

    code = await commands.run(OutboxRequest(action="cancel", row_id=row.id))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"outbox row {row.id} is {finished} and cannot be cancelled\n"
    stored = await outbox.get(row.id)
    assert stored is not None
    assert stored.state.value == finished


async def test_cancel_of_an_unknown_row_is_a_readable_error(
    commands: OutboxCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(OutboxRequest(action="cancel", row_id=FAKE_INTERNAL_ID))

    captured = capsys.readouterr()
    assert code == 1
    assert str(FAKE_INTERNAL_ID) in captured.err
    assert "does not exist" in captured.err


async def _active(contacts: SqlContactRepository, modes: SqlModeRepository) -> Contact:
    return await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)


async def _failed_row(
    outbox: SqlOutboxRepository, contact: Contact, error: str = ERROR_UNCERTAIN
) -> ScheduledMessage:
    row = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    await outbox.claim_due(FAKE_NOW)
    return await outbox.mark_failed(row.id, error)


def _sender(
    outbox: SqlOutboxRepository,
    contacts: SqlContactRepository,
    transport: ScriptedTransport,
    clock: FakeClock,
) -> OutboxSender:
    factory = outbox._session_factory
    return OutboxSender(
        outbox,
        contacts,
        SqlSenderStateRepository(factory),
        SendLimiter(SqlMessageRepository(factory), LIMITS),
        free_schedule(),
        transport,
        RecordingNotifier(),
        SendGuard(_Switch(False)),
        OUTBOX_NOTICES,
        clock=clock,
    )


@pytest.mark.parametrize("error", [ERROR_UNCERTAIN, "interrupted", "FakeBlockedError"])
async def test_retry_with_yes_schedules_a_failed_row_again_and_prints_only_the_id_and_the_time(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
    error: str,
) -> None:
    row = await _failed_row(outbox, await _active(contacts, modes), error)

    code = await commands.run(OutboxRequest(action="retry", row_id=row.id, in_seconds=30, yes=True))

    captured = capsys.readouterr()
    stored = await outbox.get(row.id)
    assert stored is not None
    assert code == 0
    assert stored.state is OutboxState.SCHEDULED
    assert stored.send_at == FAKE_NOW + timedelta(seconds=30)
    assert (stored.error_class, stored.cancel_reason, stored.claimed_at) == (None, None, None)
    assert captured.err == ""
    assert captured.out == f"outbox row {row.id} planned for {stored.send_at.isoformat()}\n"
    assert_absent(TEXT, captured.out)
    assert_absent(FAKE_TELEGRAM_ID, captured.out)


async def test_retry_uses_the_default_delay_of_five_seconds(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
) -> None:
    row = await _failed_row(outbox, await _active(contacts, modes))

    await commands.run(OutboxRequest(action="retry", row_id=row.id, yes=True))

    stored = await outbox.get(row.id)
    assert stored is not None
    assert stored.send_at == FAKE_NOW + timedelta(seconds=5)


async def test_retry_without_yes_reminds_to_check_the_chat_and_changes_nothing(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = await _failed_row(outbox, await _active(contacts, modes))

    code = await commands.run(OutboxRequest(action="retry", row_id=row.id))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "check the chat" in captured.err
    assert "--yes" in captured.err
    assert_absent(TEXT, captured.err)
    assert await outbox.get(row.id) == row


@pytest.mark.parametrize(
    "state",
    [OutboxState.SCHEDULED, OutboxState.SENDING, OutboxState.SENT, OutboxState.CANCELLED],
)
async def test_retry_refuses_every_state_except_failed(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
    state: OutboxState,
) -> None:
    contact = await _active(contacts, modes)
    row = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    if state is OutboxState.CANCELLED:
        await outbox.mark_cancelled(row.id, "fake_reason")
    elif state is not OutboxState.SCHEDULED:
        await outbox.claim_due(FAKE_NOW)
    if state is OutboxState.SENT:
        await outbox.complete_send(row.id, contact.id, TEXT, 700001, FAKE_NOW)
    before = await outbox.get(row.id)

    code = await commands.run(OutboxRequest(action="retry", row_id=row.id, yes=True))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"outbox row {row.id} is {state.value} and cannot be retried\n"
    assert await outbox.get(row.id) == before


async def test_retry_of_an_unknown_row_is_a_readable_error(
    commands: OutboxCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(OutboxRequest(action="retry", row_id=FAKE_INTERNAL_ID, yes=True))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"outbox row {FAKE_INTERNAL_ID} does not exist\n"


@pytest.mark.parametrize("seconds", [-1, MAX_IN_SECONDS + 1])
@pytest.mark.parametrize("yes", [True, False])
async def test_retry_refuses_a_delay_out_of_range(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
    seconds: int,
    yes: bool,
) -> None:
    row = await _failed_row(outbox, await _active(contacts, modes))

    code = await commands.run(
        OutboxRequest(action="retry", row_id=row.id, in_seconds=seconds, yes=yes)
    )

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "--in-seconds" in captured.err
    assert await outbox.get(row.id) == row


@pytest.mark.parametrize("seconds", [0, MAX_IN_SECONDS])
async def test_retry_accepts_the_delay_boundaries(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    seconds: int,
) -> None:
    row = await _failed_row(outbox, await _active(contacts, modes))

    request = OutboxRequest(action="retry", row_id=row.id, in_seconds=seconds, yes=True)
    assert await commands.run(request) == 0

    stored = await outbox.get(row.id)
    assert stored is not None
    assert stored.send_at == FAKE_NOW + timedelta(seconds=seconds)


async def test_a_retried_row_is_sent_exactly_once(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
) -> None:
    row = await _failed_row(outbox, await _active(contacts, modes))
    await commands.run(OutboxRequest(action="retry", row_id=row.id, yes=True))
    clock = FakeClock()
    transport = ScriptedTransport()
    sender = _sender(outbox, contacts, transport, clock)

    assert await sender.send_next() is False
    assert transport.calls == []
    clock.advance(DEFAULT_IN_SECONDS)
    assert await sender.send_next() is True
    clock.advance(86400)
    assert await sender.send_next() is False

    stored = await outbox.get(row.id)
    assert stored is not None
    assert stored.state is OutboxState.SENT
    assert transport.calls == [(FAKE_TELEGRAM_ID, TEXT)]


async def test_the_retry_command_resets_the_due_time_so_an_old_row_is_sent_and_not_stale(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
) -> None:
    contact = await _active(contacts, modes)
    old = FAKE_NOW - timedelta(seconds=ROW_STALE_AFTER_SECONDS + 1)
    row = await outbox.enqueue(contact.id, TEXT, old, OutboxSource.MANUAL_TEST)
    await outbox.claim_due(old)
    await outbox.mark_failed(row.id, ERROR_UNCERTAIN)

    assert await commands.run(OutboxRequest(action="retry", row_id=row.id, yes=True)) == 0

    planned = FAKE_NOW + timedelta(seconds=DEFAULT_IN_SECONDS)
    retried = await outbox.get(row.id)
    assert retried is not None
    assert (retried.send_at, retried.due_at) == (planned, planned)
    transport = ScriptedTransport()
    await _sender(outbox, contacts, transport, FakeClock(planned)).send_next()
    sent = await outbox.get(row.id)
    assert sent is not None
    assert sent.state is OutboxState.SENT
    assert transport.calls == [(FAKE_TELEGRAM_ID, TEXT)]


async def test_a_retried_row_that_fails_again_needs_a_new_retry(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
) -> None:
    row = await _failed_row(outbox, await _active(contacts, modes))
    await commands.run(OutboxRequest(action="retry", row_id=row.id, in_seconds=0, yes=True))
    clock = FakeClock()
    transport = ScriptedTransport([SendUncertain("FakeNetworkError")])
    sender = _sender(outbox, contacts, transport, clock)

    await sender.send_next()
    clock.advance(86400)
    assert await sender.send_next() is False

    stored = await outbox.get(row.id)
    assert stored is not None
    assert (stored.state, stored.error_class) == (OutboxState.FAILED, ERROR_UNCERTAIN)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [ContactStatus.PAUSED, ContactStatus.IGNORED])
async def test_the_guard_still_cancels_a_retried_row_when_the_contact_is_no_longer_active(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    status: ContactStatus,
) -> None:
    contact = await _active(contacts, modes)
    row = await _failed_row(outbox, contact)
    await commands.run(OutboxRequest(action="retry", row_id=row.id, in_seconds=0, yes=True))
    await contacts.set_status(contact.id, status)
    transport = ScriptedTransport()

    await _sender(outbox, contacts, transport, FakeClock()).send_next()

    stored = await outbox.get(row.id)
    assert stored is not None
    assert (stored.state, stored.cancel_reason) == (
        OutboxState.CANCELLED,
        REFUSED_CONTACT_NOT_ACTIVE,
    )
    assert transport.calls == []


async def test_enqueue_counts_the_length_in_utf16_units(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await _active(contacts, modes)
    emoji = "\N{GRINNING FACE}"

    assert await commands.run(_enqueue(contact.id, text=emoji * 2048)) == 0
    capsys.readouterr()
    assert await commands.run(_enqueue(contact.id, text=emoji * 2049)) == 1
    assert capsys.readouterr().err == f"outbox enqueue refused: {REFUSED_TEXT_TOO_LONG}\n"


@pytest.fixture
def through_main(migrated_url: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> URL:
    monkeypatch.chdir(tmp_path)
    return migrated_url


async def _active_contact_through(url: URL) -> int:
    await fetch_all(
        url,
        "INSERT INTO contacts (tg_id, status, mode_id) "  # noqa: S608
        f"SELECT {FAKE_TELEGRAM_ID}, 'active', id FROM modes LIMIT 1 RETURNING id",
    )
    rows = await fetch_all(url, "SELECT id FROM contacts")
    return int(str(rows[0][0]))


def test_through_main_the_switch_off_does_not_block_a_manual_row(
    through_main: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = asyncio.run(_active_contact_through(through_main))

    code = main(["outbox", "enqueue", str(contact_id), TEXT, "--in-seconds", "60"])

    captured = capsys.readouterr()
    assert code == 0
    assert_absent(TEXT, captured.out + captured.err)
    assert captured.out.startswith("outbox row 1 planned for ")

    assert main(["outbox", "list"]) == 0
    captured = capsys.readouterr()
    assert "state=scheduled" in captured.out
    assert_absent(TEXT, captured.out + captured.err)

    assert main(["outbox", "cancel", "1"]) == 0
    assert "cancelled" in capsys.readouterr().out


def test_through_main_retry_needs_yes_and_then_schedules_the_failed_row(
    through_main: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = asyncio.run(_active_contact_through(through_main))
    assert main(["outbox", "enqueue", str(contact_id), TEXT, "--in-seconds", "0"]) == 0
    capsys.readouterr()
    asyncio.run(execute(through_main, "UPDATE scheduled_messages SET state = 'failed'"))

    assert main(["outbox", "retry", "1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "check the chat" in captured.err

    assert main(["outbox", "retry", "1", "--in-seconds", "3600", "--yes"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("outbox row 1 planned for ")
    assert_absent(TEXT, captured.out + captured.err)
    rows = asyncio.run(fetch_all(through_main, "SELECT state, error_class FROM scheduled_messages"))
    assert rows == [("scheduled", None)]

    assert main(["outbox", "retry", "1", "--yes"]) == 1
    assert capsys.readouterr().err == "outbox row 1 is scheduled and cannot be retried\n"


def test_through_main_a_text_after_the_separator_may_start_with_a_dash(
    through_main: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = asyncio.run(_active_contact_through(through_main))

    code = main(["outbox", "enqueue", "--in-seconds", "60", str(contact_id), "--", "-dash"])

    assert code == 0
    rows = asyncio.run(fetch_all(through_main, "SELECT text FROM scheduled_messages"))
    assert rows == [("-dash",)]


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["unknown"],
        ["enqueue"],
        ["enqueue", "1"],
        ["enqueue", "not-a-number", TEXT],
        ["enqueue", "1", TEXT, "--in-seconds", "soon"],
        ["enqueue", "1", TEXT, "extra-word-marker-zq"],
        ["enqueue", "1", "-dash-word-marker-zq"],
        ["list", "--state", "unknown"],
        ["list", "--limit", "many"],
        ["cancel"],
        ["cancel", "x"],
        ["retry"],
        ["retry", "x"],
        ["retry", "x", "--yes"],
        ["retry", "1", "--in-seconds", "soon"],
        ["retry", "1", "--in-seconds"],
        ["retry", "1", "extra-word-marker-zq"],
        ["retry", "1", "--yes=please"],
        ["status", "extra-word-marker-zq"],
        ["status", "--yes"],
        ["resume", "extra-word-marker-zq"],
        ["resume", "--yes=please"],
    ],
)
def test_a_bad_command_line_prints_the_usage_and_exits_with_one_without_echoing(
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(["outbox", *arguments])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"{USAGE}\n"
    assert_absent(TEXT, captured.err)
    assert_absent("extra-word-marker-zq", captured.err)
    assert_absent("dash-word-marker-zq", captured.err)


def test_an_unreachable_database_is_a_readable_error_without_a_text(
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:x@127.0.0.1:1/none")

    code = main(["outbox", "enqueue", "1", TEXT])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("storage failed: ")
    assert_absent(TEXT, captured.out + captured.err)


def test_a_missing_configuration_exits_with_the_usual_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["outbox", "list"]) == 2
    assert "Missing required environment variables" in capsys.readouterr().err


async def _outgoing(
    messages: SqlMessageRepository,
    outbox: SqlOutboxRepository,
    contact: Contact,
    sent_at: datetime,
    tg_message_id: int,
) -> None:
    row = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    claimed = await outbox.claim_due(FAKE_NOW)
    assert claimed is not None
    assert claimed.id == row.id
    await outbox.complete_send(row.id, contact.id, TEXT, tg_message_id, sent_at)


async def test_status_on_a_quiet_sender_shows_running_empty_counters_and_every_state(
    commands: OutboxCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(OutboxRequest(action="status"))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "sender: running",
        "global minute 0/4",
        "global hour 0/40",
        "global day 0/200",
        "contact minute 0/2",
        "contact hour 0/15",
        "contact day 0/60",
        "rows: scheduled=0 sending=0 sent=0 failed=0 cancelled=0",
    ]


async def test_status_counts_each_window_against_its_limit_and_names_the_busiest_contact(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await _active(contacts, modes)
    other = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID + 1)
    await _outgoing(messages, outbox, contact, FAKE_NOW - timedelta(seconds=10), 1)
    await _outgoing(messages, outbox, other, FAKE_NOW - timedelta(minutes=10), 2)
    await _outgoing(messages, outbox, other, FAKE_NOW - timedelta(hours=5), 3)
    await _outgoing(messages, outbox, other, FAKE_NOW - timedelta(days=2), 4)
    await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)

    code = await commands.run(OutboxRequest(action="status"))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == [
        "sender: running",
        "global minute 1/4",
        "global hour 2/40",
        "global day 3/200",
        f"contact minute 1/2 (contact {contact.id})",
        f"contact hour 1/15 (contact {contact.id})",
        f"contact day 2/60 (contact {other.id})",
        "rows: scheduled=1 sending=0 sent=4 failed=0 cancelled=0",
    ]
    assert_absent(TEXT, captured.out + captured.err)
    assert_absent(FAKE_TELEGRAM_ID, captured.out + captured.err)


async def test_status_treats_a_message_exactly_one_window_old_as_outside_the_window(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    outbox: SqlOutboxRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await _active(contacts, modes)
    await _outgoing(messages, outbox, contact, FAKE_NOW - timedelta(seconds=60), 1)

    await commands.run(OutboxRequest(action="status"))

    out = capsys.readouterr().out
    assert "global minute 0/4" in out
    assert "contact minute 0/2" in out
    assert "global hour 1/40" in out


async def test_status_shows_a_pause_with_its_time_and_treats_an_expired_one_as_running(
    commands: OutboxCommands,
    sender_state: SqlSenderStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    until = FAKE_NOW + timedelta(seconds=90)
    await sender_state.pause_until(until, "FakeFloodWait", now=FAKE_NOW)

    await commands.run(OutboxRequest(action="status"))
    assert (
        capsys.readouterr().out.splitlines()[0]
        == f"sender: paused until {until.isoformat()} reason=FakeFloodWait"
    )

    await sender_state.resume()
    await sender_state.pause_until(FAKE_NOW, "FakeFloodWait", now=FAKE_NOW)
    await commands.run(OutboxRequest(action="status"))
    assert capsys.readouterr().out.splitlines()[0] == "sender: running"


async def test_status_shows_a_stop_with_its_reason_and_a_pause_next_to_it(
    commands: OutboxCommands,
    sender_state: SqlSenderStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sender_state.stop("FakeAccountProblem")

    await commands.run(OutboxRequest(action="status"))
    assert capsys.readouterr().out.splitlines()[0] == "sender: stopped reason=FakeAccountProblem"

    until = FAKE_NOW + timedelta(seconds=90)
    await sender_state.pause_until(until, "FakeFloodWait", now=FAKE_NOW)
    await commands.run(OutboxRequest(action="status"))
    assert capsys.readouterr().out.splitlines()[:2] == [
        "sender: stopped reason=FakeAccountProblem",
        f"sender: paused until {until.isoformat()} reason=FakeFloodWait",
    ]


@pytest.mark.parametrize("yes", [True, False])
async def test_resume_with_nothing_to_clear_is_harmless(
    commands: OutboxCommands,
    sender_state: SqlSenderStateRepository,
    capsys: pytest.CaptureFixture[str],
    yes: bool,
) -> None:
    code = await commands.run(OutboxRequest(action="resume", yes=yes))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert captured.out == "nothing to clear: the sender is running\n"


async def test_resume_says_nothing_was_cleared_for_a_pause_that_already_ended(
    commands: OutboxCommands,
    sender_state: SqlSenderStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sender_state.pause_until(FAKE_NOW, "FakeFloodWait", now=FAKE_NOW)

    code = await commands.run(OutboxRequest(action="resume"))

    assert code == 0
    assert capsys.readouterr().out == "nothing to clear: the sender is running\n"


async def test_resume_clears_a_pause_without_yes_and_says_so(
    commands: OutboxCommands,
    sender_state: SqlSenderStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    until = FAKE_NOW + timedelta(seconds=90)
    await sender_state.pause_until(until, "FakeFloodWait", now=FAKE_NOW)

    code = await commands.run(OutboxRequest(action="resume"))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == f"pause cleared, it was until {until.isoformat()} reason=FakeFloodWait\n"
    assert (await sender_state.get()).paused_until is None


async def test_resume_without_yes_never_clears_a_stop_and_reminds_to_check_the_account(
    commands: OutboxCommands,
    sender_state: SqlSenderStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sender_state.stop("FakeAccountProblem")
    until = FAKE_NOW + timedelta(seconds=90)
    await sender_state.pause_until(until, "FakeFloodWait", now=FAKE_NOW)

    code = await commands.run(OutboxRequest(action="resume"))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "check the account" in captured.err
    assert "--yes" in captured.err
    assert "FakeAccountProblem" in captured.err
    state = await sender_state.get()
    assert state.stopped is True
    assert state.paused_until == until


async def test_resume_with_yes_clears_a_stop_and_a_pause_and_says_what_was_cleared(
    commands: OutboxCommands,
    sender_state: SqlSenderStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await sender_state.stop("FakeAccountProblem")
    until = FAKE_NOW + timedelta(seconds=90)
    await sender_state.pause_until(until, "FakeFloodWait", now=FAKE_NOW)

    code = await commands.run(OutboxRequest(action="resume", yes=True))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == [
        "stop cleared, the reason was FakeAccountProblem",
        f"pause cleared, it was until {until.isoformat()} reason=FakeFloodWait",
    ]
    state = await sender_state.get()
    assert (state.stopped, state.stop_reason, state.paused_until, state.pause_reason) == (
        False,
        None,
        None,
        None,
    )


async def test_a_resumed_sender_sends_again(
    commands: OutboxCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    outbox: SqlOutboxRepository,
    sender_state: SqlSenderStateRepository,
) -> None:
    contact = await _active(contacts, modes)
    row = await outbox.enqueue(contact.id, TEXT, FAKE_NOW, OutboxSource.MANUAL_TEST)
    await sender_state.stop("FakeAccountProblem")
    transport = ScriptedTransport()
    sender = _sender(outbox, contacts, transport, FakeClock())

    assert await sender.send_next() is False
    await commands.run(OutboxRequest(action="resume"))
    assert await sender.send_next() is False
    await commands.run(OutboxRequest(action="resume", yes=True))
    assert await sender.send_next() is True

    stored = await outbox.get(row.id)
    assert stored is not None
    assert stored.state is OutboxState.SENT
    assert len(transport.calls) == 1


def test_through_main_status_and_resume_work_with_the_real_settings(
    through_main: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["outbox", "status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("sender: running\nglobal minute 0/4\n")
    assert "contact day 0/60\n" in out

    asyncio.run(
        execute(through_main, "UPDATE sender_state SET stopped = true, stop_reason = 'FakeKind'")
    )
    assert main(["outbox", "status"]) == 0
    assert capsys.readouterr().out.startswith("sender: stopped reason=FakeKind\n")

    assert main(["outbox", "resume"]) == 1
    assert "check the account" in capsys.readouterr().err
    assert main(["outbox", "resume", "--yes"]) == 0
    assert capsys.readouterr().out == "stop cleared, the reason was FakeKind\n"
    assert main(["outbox", "status"]) == 0
    assert capsys.readouterr().out.startswith("sender: running\n")


def test_through_main_an_unreachable_database_is_a_readable_error_for_status_and_resume(
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:x@127.0.0.1:1/none")

    assert main(["outbox", "status"]) == 1
    assert capsys.readouterr().err.startswith("storage failed: ")
    assert main(["outbox", "resume"]) == 1
    assert capsys.readouterr().err.startswith("storage failed: ")
