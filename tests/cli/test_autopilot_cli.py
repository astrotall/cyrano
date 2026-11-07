from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from cyrano.autopilot_cli import USAGE, AutopilotCommands, AutopilotRequest
from cyrano.db.repositories import (
    SqlAutopilotStateRepository,
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlReplyBacklogRepository,
)
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactStatus,
    MessageDirection,
    OutboxSource,
)
from cyrano.domain.models import DEFAULT_MODE_NAME, ContactCreate, MessageCreate
from cyrano.main import main
from tests.db.conftest import fetch_all
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
TEXT_ZQ = "autopilot-cli-text-marker-zq"
NAME_ZQ = "Autopilot-Cli-Name-zq"
UNAVAILABLE = AutopilotPauseReason.PROVIDER_UNAVAILABLE


@pytest.fixture
def state(session_factory: SessionFactory) -> SqlAutopilotStateRepository:
    return SqlAutopilotStateRepository(session_factory)


def _commands(session_factory: SessionFactory, *, enabled: bool = True) -> AutopilotCommands:
    return AutopilotCommands(
        SqlAutopilotStateRepository(session_factory),
        SqlOutboxRepository(session_factory),
        SqlReplyBacklogRepository(session_factory),
        enabled=enabled,
        clock=lambda: NOW,
    )


async def _active_contact_with_a_message(session_factory: SessionFactory) -> int:
    contacts = SqlContactRepository(session_factory)
    modes = SqlModeRepository(session_factory)
    contact = await contacts.create(ContactCreate(tg_id=FAKE_TELEGRAM_ID, display_name=NAME_ZQ))
    await contacts.set_status(contact.id, ContactStatus.PENDING)
    default = await modes.get_by_name(DEFAULT_MODE_NAME)
    assert default is not None
    await contacts.set_status(contact.id, ContactStatus.ACTIVE, mode_id=default.id)
    await SqlMessageRepository(session_factory).append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.INCOMING,
            text=TEXT_ZQ,
            tg_message_id=5,
        )
    )
    return contact.id


async def test_status_shows_the_switch_the_state_the_rows_and_the_unanswered_count(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _active_contact_with_a_message(session_factory)
    outbox = SqlOutboxRepository(session_factory)
    await outbox.enqueue(contact_id, TEXT_ZQ, NOW, OutboxSource.AUTOPILOT, 99)
    await outbox.enqueue(contact_id, TEXT_ZQ, NOW, OutboxSource.MANUAL_TEST)

    code = await _commands(session_factory).run(AutopilotRequest(action="status"))

    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines() == [
        "switch: AUTOPILOT_ENABLED=true",
        "autopilot: running",
        "autopilot rows: scheduled=1 sending=0 sent=0 failed=0 cancelled=0",
        "active contacts with an unanswered incoming message: 1",
    ]
    for marker in (TEXT_ZQ, NAME_ZQ, FAKE_TELEGRAM_ID):
        assert_absent(marker, out)


async def test_status_shows_a_stop_and_a_pause_with_their_reasons(
    session_factory: SessionFactory,
    state: SqlAutopilotStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await state.stop(AutopilotStopReason.PROVIDER_REQUEST)
    await state.pause_until(NOW + timedelta(minutes=15), UNAVAILABLE, now=NOW)

    await _commands(session_factory, enabled=False).run(AutopilotRequest(action="status"))

    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == [
        "switch: AUTOPILOT_ENABLED=false",
        "autopilot: stopped reason=provider_request",
        "autopilot: paused until 2026-11-04T12:15:00+00:00 reason=provider_unavailable",
    ]


async def test_an_expired_pause_shows_as_running(
    session_factory: SessionFactory,
    state: SqlAutopilotStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await state.pause_until(NOW, UNAVAILABLE, now=NOW - timedelta(minutes=1))

    await _commands(session_factory).run(AutopilotRequest(action="status"))

    assert "autopilot: running" in capsys.readouterr().out.splitlines()


async def test_resume_clears_a_pause(
    session_factory: SessionFactory,
    state: SqlAutopilotStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await state.pause_until(NOW + timedelta(minutes=15), UNAVAILABLE, now=NOW)

    code = await _commands(session_factory).run(AutopilotRequest(action="resume"))

    assert code == 0
    assert capsys.readouterr().out == (
        "pause cleared, it was until 2026-11-04T12:15:00+00:00 reason=provider_unavailable\n"
    )
    assert (await state.get()).is_running(NOW)


async def test_resume_without_yes_keeps_a_stop_and_exits_with_one(
    session_factory: SessionFactory,
    state: SqlAutopilotStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await state.stop(AutopilotStopReason.PROVIDER_AUTH)
    await state.pause_until(NOW + timedelta(minutes=15), UNAVAILABLE, now=NOW)

    code = await _commands(session_factory).run(AutopilotRequest(action="resume"))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "reason=provider_auth" in captured.err
    assert "--yes" in captured.err
    current = await state.get()
    assert current.stopped
    assert current.is_paused(NOW)


async def test_resume_with_yes_clears_the_stop_and_the_pause(
    session_factory: SessionFactory,
    state: SqlAutopilotStateRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await state.stop(AutopilotStopReason.PERSONA_MISSING)
    await state.pause_until(NOW + timedelta(minutes=15), UNAVAILABLE, now=NOW)

    code = await _commands(session_factory).run(AutopilotRequest(action="resume", yes=True))

    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        "stop cleared, the reason was persona_missing",
        "pause cleared, it was until 2026-11-04T12:15:00+00:00 reason=provider_unavailable",
    ]
    assert (await state.get()).is_running(NOW)


@pytest.mark.parametrize("yes", [False, True])
async def test_resume_with_nothing_to_clear_is_harmless(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str], yes: bool
) -> None:
    code = await _commands(session_factory).run(AutopilotRequest(action="resume", yes=yes))

    assert code == 0
    assert capsys.readouterr().out == "nothing to clear: the autopilot is running\n"


@pytest.fixture
def through_main(migrated_url: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> URL:
    monkeypatch.chdir(tmp_path)
    return migrated_url


def test_through_main_status_and_resume_work_on_the_database(
    through_main: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(
        fetch_all(
            through_main,
            "UPDATE autopilot_state SET stopped = true, stop_reason = 'provider_auth' RETURNING id",
        )
    )

    assert main(["autopilot", "status"]) == 0
    assert "autopilot: stopped reason=provider_auth" in capsys.readouterr().out
    assert main(["autopilot", "resume"]) == 1
    capsys.readouterr()
    assert main(["autopilot", "resume", "--yes"]) == 0
    assert capsys.readouterr().out == "stop cleared, the reason was provider_auth\n"
    assert main(["autopilot", "status"]) == 0
    assert "autopilot: running" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [[], ["stop"], ["status", "extra-word-marker-zq"], ["resume", "--yes=please"]],
)
def test_a_bad_command_line_prints_the_usage_and_exits_with_one(
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(["autopilot", *arguments])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"{USAGE}\n"


def test_an_unreachable_database_is_a_readable_error(
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:x@127.0.0.1:1/none")

    code = main(["autopilot", "status"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("storage failed: ")


def test_a_missing_configuration_exits_with_the_usual_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["autopilot", "status"]) == 2
