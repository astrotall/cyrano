from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from cyrano.bot.notifier import OUTBOX_NOTICES
from cyrano.db.repositories import SqlContactRepository, SqlModeRepository, SqlOutboxRepository
from cyrano.domain.enums import ContactStatus, OutboxSource, OutboxState
from cyrano.domain.errors import SendUncertain
from cyrano.domain.models import Contact
from cyrano.domain.outbox import (
    CANCEL_REASON_OWNER,
    ERROR_UNCERTAIN,
    MAX_OUTBOX_TEXT_LENGTH,
    REFUSED_CONTACT_NOT_ACTIVE,
    REFUSED_TEXT_EMPTY,
    REFUSED_TEXT_TOO_LONG,
    REFUSED_UNKNOWN_CONTACT,
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
from tests.db.conftest import execute, fetch_all
from tests.privacy import FAKE_INTERNAL_ID, FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import (
    FAKE_NOW,
    FakeClock,
    RecordingNotifier,
    ScriptedTransport,
    stored_contact,
)

TEXT = "cli-text-marker-zq must never be printed"


@dataclass(frozen=True)
class _Switch:
    autopilot_enabled: bool


@pytest.fixture
def commands(contacts: SqlContactRepository, outbox: SqlOutboxRepository) -> OutboxCommands:
    service = OutboxService(contacts, outbox, SendGuard(_Switch(False)))
    return OutboxCommands(service, outbox, clock=FakeClock())


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
        f"send_at={FAKE_NOW.isoformat()} error=FakeError\n"
    )
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
    return OutboxSender(
        outbox,
        contacts,
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
