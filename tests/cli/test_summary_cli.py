from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlSummaryRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactGender, ContactStatus, MessageDirection
from cyrano.domain.errors import LLMUnavailableError
from cyrano.domain.models import DEFAULT_MODE_NAME, ContactCreate, ContactUpdate, MessageCreate
from cyrano.domain.summary import (
    SUMMARY_VERSION,
    ContactSummary,
    StoredSummary,
    SummaryCursor,
    read_stored_summary,
)
from cyrano.main import main
from cyrano.services.summary_updater import SummaryUpdater
from cyrano.summary_cli import (
    PERSONAL_DATA_WARNING,
    USAGE,
    SummaryCommands,
    SummaryRequest,
)
from tests.db.conftest import fetch_all
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.summary_fakes import JsonStep, ScriptedJsonClient

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
TEXT_MARKER = "summary-cli-text-zq"
NAME_MARKER = "Summary-Cli-Name-zq"
VALUE_MARKER = "summary-cli-value-zq"
REPLY = ContactSummary(name=VALUE_MARKER, interests=["кино"], told_about_owner=["владелец инженер"])


async def _contact(
    session_factory: SessionFactory, status: ContactStatus = ContactStatus.ACTIVE
) -> int:
    contacts = SqlContactRepository(session_factory)
    contact = await contacts.create(ContactCreate(tg_id=FAKE_TELEGRAM_ID, display_name=NAME_MARKER))
    if status is ContactStatus.NEW:
        return contact.id
    await contacts.set_status(contact.id, ContactStatus.PENDING)
    if status is ContactStatus.PENDING:
        return contact.id
    default = await SqlModeRepository(session_factory).get_by_name(DEFAULT_MODE_NAME)
    assert default is not None
    await contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=default.id)
    messages = SqlMessageRepository(session_factory)
    for number in range(3):
        await messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=MessageDirection.INCOMING,
                text=f"{TEXT_MARKER} {number}",
                tg_message_id=number + 1,
                created_at=NOW - timedelta(minutes=10 - number),
            )
        )
    return contact.id


def _commands(session_factory: SessionFactory, *steps: JsonStep) -> SummaryCommands:
    contacts = SqlContactRepository(session_factory)
    summaries = SqlSummaryRepository(session_factory)
    updater = SummaryUpdater(
        contacts=contacts,
        summaries=summaries,
        client=ScriptedJsonClient(*steps),
        clock=lambda: NOW,
    )
    return SummaryCommands(updater, contacts, summaries)


async def _stored(session_factory: SessionFactory, contact_id: int) -> StoredSummary | None:
    contact = await SqlContactRepository(session_factory).get_by_id(contact_id)
    assert contact is not None
    return read_stored_summary(contact.summary)


async def test_update_runs_inline_and_prints_the_outcome_and_counts(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _contact(session_factory)

    code = await _commands(session_factory, REPLY).run(SummaryRequest("update", contact_id))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == (
        "summary update: outcome=updated reason=updated messages=3 messages_in_prompt=3 "
        "remaining=0 removed=0\n"
    )
    assert captured.err == ""
    assert await _stored(session_factory, contact_id) is not None


async def test_update_with_nothing_new_exits_zero(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _contact(session_factory)
    commands = _commands(session_factory, REPLY)
    await commands.run(SummaryRequest("update", contact_id))
    capsys.readouterr()

    code = await commands.run(SummaryRequest("update", contact_id))

    assert code == 0
    assert "outcome=nothing_to_do reason=no_new_messages messages=0" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "step", "expected"),
    [
        (ContactStatus.PENDING, REPLY, "outcome=not_eligible reason=status_not_eligible"),
        (
            ContactStatus.ACTIVE,
            LLMUnavailableError(f"down {VALUE_MARKER}"),
            "outcome=failed reason=llm_error",
        ),
    ],
)
async def test_an_ineligible_contact_or_a_failure_exits_one_on_stderr(
    session_factory: SessionFactory,
    capsys: pytest.CaptureFixture[str],
    status: ContactStatus,
    step: JsonStep,
    expected: str,
) -> None:
    contact_id = await _contact(session_factory, status)

    code = await _commands(session_factory, step).run(SummaryRequest("update", contact_id))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert expected in captured.err
    assert_absent(VALUE_MARKER, captured.err)


async def test_update_of_a_missing_contact_is_a_readable_line(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _commands(session_factory).run(SummaryRequest("update", 424242))

    assert code == 1
    assert capsys.readouterr().err == "summary update failed: no contact with this id\n"


async def test_show_prints_a_warning_on_stderr_and_the_fields_on_stdout(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _contact(session_factory)
    commands = _commands(session_factory, REPLY)
    await commands.run(SummaryRequest("update", contact_id))
    capsys.readouterr()

    code = await commands.run(SummaryRequest("show", contact_id))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == f"{PERSONAL_DATA_WARNING}\n"
    lines = captured.out.splitlines()
    assert lines[0] == f"updated_at: {NOW.isoformat()}"
    assert lines[1].startswith("covers messages up to: ")
    assert lines[2:] == [
        f"name: {VALUE_MARKER}",
        "interests:",
        "  - кино",
        "told_about_owner:",
        "  - владелец инженер",
    ]
    assert_absent(NAME_MARKER, captured.out)
    assert_absent(TEXT_MARKER, captured.out)


@pytest.mark.parametrize("raw", [{}, {"topic": "old shape"}])
async def test_show_without_a_readable_summary_says_so_and_exits_zero(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str], raw: dict[str, object]
) -> None:
    contact_id = await _contact(session_factory)
    await SqlContactRepository(session_factory).update(contact_id, ContactUpdate(summary=raw))

    code = await _commands(session_factory).run(SummaryRequest("show", contact_id))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "no summary\n"
    assert captured.err == ""


async def test_show_of_a_missing_contact_exits_one(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _commands(session_factory).run(SummaryRequest("show", 424242)) == 1
    assert capsys.readouterr().err == "summary show failed: no contact with this id\n"


async def test_clear_needs_yes_and_then_clears_the_summary_and_the_marker(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _contact(session_factory)
    summaries = SqlSummaryRepository(session_factory)
    await summaries.store_summary(
        contact_id,
        StoredSummary(
            version=SUMMARY_VERSION,
            updated_at=NOW,
            cursor=SummaryCursor(created_at=NOW, message_id=1),
            data=REPLY,
        ),
        None,
    )
    await summaries.mark_summary_failed(contact_id, NOW)
    commands = _commands(session_factory)

    refused = await commands.run(SummaryRequest("clear", contact_id))
    refused_output = capsys.readouterr()
    assert await _stored(session_factory, contact_id) is not None

    cleared = await commands.run(SummaryRequest("clear", contact_id, yes=True))
    cleared_output = capsys.readouterr().out
    again = await commands.run(SummaryRequest("clear", contact_id, yes=True))

    assert refused == 1
    assert "add --yes" in refused_output.err
    assert refused_output.out == ""
    assert (cleared, cleared_output) == (0, "summary cleared\n")
    assert (again, capsys.readouterr().out) == (0, "nothing to clear\n")
    contact = await SqlContactRepository(session_factory).get_by_id(contact_id)
    assert contact is not None
    assert (contact.summary, contact.summary_failed_at) == ({}, None)


async def test_clear_of_a_missing_contact_exits_one(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _commands(session_factory).run(SummaryRequest("clear", 424242, yes=True))

    assert code == 1
    assert capsys.readouterr().err == "summary clear failed: no contact with this id\n"


@pytest.fixture
def through_main(migrated_url: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> URL:
    monkeypatch.chdir(tmp_path)
    return migrated_url


def test_through_main_show_and_clear_work_on_the_database(
    through_main: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(
        fetch_all(
            through_main,
            "INSERT INTO contacts (tg_id, status) "  # noqa: S608
            f"VALUES ({FAKE_TELEGRAM_ID}, 'active') RETURNING id",
        )
    )
    [(contact_id,)] = asyncio.run(fetch_all(through_main, "SELECT id FROM contacts"))

    assert main(["summary", "show", str(contact_id)]) == 0
    assert capsys.readouterr().out == "no summary\n"
    assert main(["summary", "clear", str(contact_id)]) == 1
    assert main(["summary", "clear", str(contact_id), "--yes"]) == 0
    assert capsys.readouterr().out == "nothing to clear\n"
    assert main(["summary", "update", str(contact_id)]) == 0
    assert "reason=no_new_messages" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [[], ["update"], ["show", "x"], ["clear"], ["remove", "3"], ["update", "3", "--yes"]],
)
def test_a_bad_command_line_prints_the_usage_and_exits_with_one(
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(["summary", *arguments])

    assert code == 1
    assert capsys.readouterr().err == f"{USAGE}\n"


def test_a_storage_failure_prints_the_class_only(
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:x@127.0.0.1:1/none")

    code = main(["summary", "show", "3"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("storage failed: ")


def test_a_missing_configuration_exits_with_the_usual_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["summary", "show", "3"]) == 2


async def test_show_prints_the_gender_line_when_the_summary_has_one(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _contact(session_factory)
    commands = _commands(
        session_factory, ContactSummary(name=VALUE_MARKER, gender=ContactGender.FEMALE)
    )
    await commands.run(SummaryRequest("update", contact_id))
    capsys.readouterr()

    code = await commands.run(SummaryRequest("show", contact_id))

    captured = capsys.readouterr()
    assert code == 0
    assert "gender: female" in captured.out.splitlines()
