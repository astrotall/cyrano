from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlScheduleProfileRepository,
)
from cyrano.domain.enums import ContactStatus, MessageDirection
from cyrano.domain.models import MessageCreate
from cyrano.main import main
from cyrano.schedule_cli import BAD_AT, USAGE, ScheduleCommands, ScheduleRequest
from cyrano.services.humanization import HumanizationPlanner, ScheduleService
from cyrano.services.schedule_files import MAX_SCHEDULE_FILE_BYTES, ScheduleImporter
from tests.db.conftest import execute, fetch_all
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import FakeClock, stored_contact
from tests.services.schedule_data import (
    BERLIN,
    FRIDAY,
    MONDAY,
    MOSCOW,
    TUESDAY,
    at_local,
    profile_with,
)
from tests.style_samples import REPO_ROOT

SAMPLE = REPO_ROOT / "examples" / "schedule.sample.json"
NOON = at_local(TUESDAY, 12)
TEXT = "schedule-cli-text-marker-zq"
USERNAME = "schedule_cli_username_marker_zq"
DISPLAY_NAME = "ScheduleCliNameMarkerZq"
MARKER_VALUE = "schedule-cli-value-marker-zq"
DEFAULT_LINE = "the default profile is in use"
STORED_LINE = "the stored profile is in use"
VALID = {
    "version": 1,
    "timezone": BERLIN,
    "busy": [{"days": ["tue"], "from": "13:00", "to": "14:30"}],
}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOON)


@pytest.fixture
def commands(
    schedule_profiles: SqlScheduleProfileRepository,
    messages: SqlMessageRepository,
    clock: FakeClock,
) -> ScheduleCommands:
    service = ScheduleService(schedule_profiles, MOSCOW)
    return ScheduleCommands(
        ScheduleImporter(schedule_profiles),
        service,
        HumanizationPlanner(messages, service, random.Random(5)),
        schedule_profiles,
        clock=clock,
    )


def _file(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return path


def _import(path: Path) -> ScheduleRequest:
    return ScheduleRequest(action="import", path=path)


def _single_error_line(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith("\n")
    assert captured.err.count("\n") == 1
    return str(captured.err)


async def test_import_stores_the_profile_and_prints_a_short_confirmation(
    commands: ScheduleCommands,
    schedule_profiles: SqlScheduleProfileRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await commands.run(_import(_file(tmp_path, VALID)))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert captured.out == "schedule profile imported: 1 busy window(s)\n"
    stored = await schedule_profiles.get()
    assert stored is not None
    assert stored.timezone == BERLIN


async def test_the_sample_file_imports(
    commands: ScheduleCommands, schedule_profiles: SqlScheduleProfileRepository
) -> None:
    assert await commands.run(_import(SAMPLE)) == 0
    assert await schedule_profiles.get() is not None


async def test_a_second_import_replaces_the_first_as_a_whole(
    commands: ScheduleCommands,
    schedule_profiles: SqlScheduleProfileRepository,
    migrated_url: URL,
    tmp_path: Path,
) -> None:
    await commands.run(_import(_file(tmp_path, VALID)))

    assert await commands.run(_import(_file(tmp_path, {"version": 1}))) == 0

    stored = await schedule_profiles.get()
    assert stored is not None
    assert stored.busy == ()
    assert stored.timezone is None
    assert await fetch_all(migrated_url, "SELECT id FROM schedule_profile") == [(1,)]


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("{not json", "invalid JSON"),
        ("", "invalid JSON"),
        ("[]", "must be an object"),
        ({"busy": []}, "version: missing"),
        ({"version": 2}, "version: must be 1"),
        ({"version": 1, MARKER_VALUE: 1}, "(file): unknown key"),
        ({"version": 1, "timezone": MARKER_VALUE}, "timezone: must be an IANA time zone name"),
        ({"version": 1, "night": {"start": [MARKER_VALUE, "02:00"]}}, "night.start[0]"),
        ({"version": 1, "read_pause_seconds": [3, 1]}, "read_pause_seconds"),
    ],
    ids=[
        "bad_json",
        "empty",
        "not_an_object",
        "no_version",
        "bad_version",
        "unknown_key",
        "bad_timezone",
        "bad_time",
        "bad_order",
    ],
)
async def test_an_invalid_file_stores_nothing_and_says_where_in_one_line(
    commands: ScheduleCommands,
    schedule_profiles: SqlScheduleProfileRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    document: object,
    expected: str,
) -> None:
    code = await commands.run(_import(_file(tmp_path, document)))

    line = _single_error_line(capsys)
    assert code == 1
    assert expected in line
    assert_absent(MARKER_VALUE, line)
    assert await schedule_profiles.get() is None


async def test_an_invalid_file_keeps_the_stored_profile(
    commands: ScheduleCommands,
    schedule_profiles: SqlScheduleProfileRepository,
    tmp_path: Path,
) -> None:
    await commands.run(_import(_file(tmp_path, VALID)))
    before = await schedule_profiles.get()

    assert await commands.run(_import(_file(tmp_path, {"version": 3}))) == 1

    assert await schedule_profiles.get() == before


async def test_several_problems_still_fit_one_line(
    commands: ScheduleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = {"version": 1, "timezone": "Mars/Olympus", "read_pause_seconds": [3, 1]}

    await commands.run(_import(_file(tmp_path, document)))

    line = _single_error_line(capsys)
    assert "timezone" in line
    assert "read_pause_seconds" in line


async def test_a_missing_file_is_a_readable_error(
    commands: ScheduleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await commands.run(_import(tmp_path / "absent.json"))

    assert code == 1
    assert "not a readable file" in _single_error_line(capsys)


async def test_a_file_over_the_limit_is_a_readable_error(
    commands: ScheduleCommands,
    schedule_profiles: SqlScheduleProfileRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "large.json"
    path.write_text('{"version": 1}' + " " * MAX_SCHEDULE_FILE_BYTES)

    code = await commands.run(_import(path))

    assert code == 1
    assert "larger than" in _single_error_line(capsys)
    assert await schedule_profiles.get() is None


async def test_a_non_utf8_file_is_a_readable_error(
    commands: ScheduleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.json"
    path.write_bytes(b"\xff\xfe\x00")

    assert await commands.run(_import(path)) == 1
    assert "UTF-8" in _single_error_line(capsys)


async def test_show_says_when_the_default_profile_is_in_use(
    commands: ScheduleCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await commands.run(ScheduleRequest(action="show")) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0] == DEFAULT_LINE
    assert f"timezone: {MOSCOW}" in out
    assert "night start: 01:00-02:00" in out
    assert "night end, Monday to Friday: 09:00-09:00" in out
    assert "night end, Saturday and Sunday: 11:00-12:00" in out
    assert "busy windows: none" in out
    assert "reaction, ongoing: 1-4 s; new: 20-120 s; ongoing window: 10 min" in out
    assert "read pause: 1-3 s" in out
    assert "typing: 5 characters per second, 2-25 s" in out
    assert "postponed spread: 1-15 min" in out
    assert "split pause: 3-8 s" in out
    assert out.splitlines()[-1] == "early read: 0-1 s"


async def test_show_prints_the_stored_profile(
    commands: ScheduleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands.run(_import(_file(tmp_path, VALID)))
    capsys.readouterr()

    assert await commands.run(ScheduleRequest(action="show")) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[0] == STORED_LINE
    assert f"timezone: {BERLIN}" in out
    assert "busy windows: tue 13:00-14:30" in out


async def test_show_prints_a_stored_split_pause(
    commands: ScheduleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands.run(_import(_file(tmp_path, {**VALID, "split_pause_seconds": [5, 9]})))
    capsys.readouterr()

    assert await commands.run(ScheduleRequest(action="show")) == 0

    assert "split pause: 5-9 s" in capsys.readouterr().out


async def test_show_prints_a_stored_early_read_range(
    commands: ScheduleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands.run(_import(_file(tmp_path, {**VALID, "early_read_seconds": [0, 0]})))
    capsys.readouterr()

    assert await commands.run(ScheduleRequest(action="show")) == 0

    assert "early read: 0-0 s" in capsys.readouterr().out


async def test_show_prints_the_wrapup_minutes_and_the_busy_labels(
    commands: ScheduleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = {
        **VALID,
        "wrapup_soft_minutes": 45,
        "wrapup_hard_minutes": 15,
        "busy": [{"days": ["tue"], "from": "13:00", "to": "14:30", "label": "лекция"}],
    }
    await commands.run(_import(_file(tmp_path, document)))
    capsys.readouterr()

    assert await commands.run(ScheduleRequest(action="show")) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "wrap-up: soft 45 min, hard 15 min" in lines
    assert "busy windows: tue 13:00-14:30 (лекция)" in lines


async def test_show_prints_the_default_wrapup_minutes(
    commands: ScheduleCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await commands.run(ScheduleRequest(action="show")) == 0

    assert "wrap-up: soft 60 min, hard 20 min" in capsys.readouterr().out.splitlines()


async def test_show_prints_the_default_proactive_window(
    commands: ScheduleCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await commands.run(ScheduleRequest(action="show")) == 0

    assert "proactive window: 10:00-22:00" in capsys.readouterr().out.splitlines()


async def test_show_prints_a_stored_proactive_window(
    commands: ScheduleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands.run(_import(_file(tmp_path, {**VALID, "proactive_window": ["11:30", "20:15"]})))
    capsys.readouterr()

    assert await commands.run(ScheduleRequest(action="show")) == 0

    assert "proactive window: 11:30-20:15" in capsys.readouterr().out.splitlines()


async def test_a_broken_proactive_window_is_refused_with_its_path_and_nothing_is_stored(
    commands: ScheduleCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _file(tmp_path, {**VALID, "proactive_window": ["22:00", "10:00"]})

    assert await commands.run(_import(path)) == 1

    err = capsys.readouterr().err
    assert "proactive_window" in err
    assert "22:00" not in err
    assert await commands.run(ScheduleRequest(action="show")) == 0
    assert capsys.readouterr().out.splitlines()[0] == DEFAULT_LINE


async def test_the_sample_file_imports_and_shows_its_early_read_range(
    commands: ScheduleCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await commands.run(_import(SAMPLE)) == 0
    capsys.readouterr()

    assert await commands.run(ScheduleRequest(action="show")) == 0

    assert "early read: 0-1 s" in capsys.readouterr().out


async def test_check_says_free_at_a_free_moment(
    commands: ScheduleCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await commands.run(ScheduleRequest(action="check")) == 0
    assert capsys.readouterr().out == "free\n"


async def test_check_uses_now_by_default_and_reports_the_night(
    commands: ScheduleCommands, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    clock.now = at_local(TUESDAY, 5)

    assert await commands.run(ScheduleRequest(action="check")) == 0

    assert capsys.readouterr().out == f"blocked until 2026-11-03 09:00 ({MOSCOW}), kind=night\n"


async def test_check_reads_at_in_the_profile_timezone(
    commands: ScheduleCommands,
    schedule_profiles: SqlScheduleProfileRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await schedule_profiles.replace(
        profile_with(timezone=BERLIN, busy=[{"days": ["fri"], "from": "13:00", "to": "14:30"}])
    )
    request = ScheduleRequest(action="check", at=f"{FRIDAY.isoformat()} 13:30")

    assert await commands.run(request) == 0

    assert capsys.readouterr().out == f"blocked until 2026-11-06 14:30 ({BERLIN}), kind=busy\n"


@pytest.mark.parametrize(
    "value", ["tomorrow", "2026-11-03", "2026-13-40 10:00", "10:00", "", MARKER_VALUE]
)
async def test_check_refuses_a_bad_at_in_one_line(
    commands: ScheduleCommands, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    code = await commands.run(ScheduleRequest(action="check", at=value))

    line = _single_error_line(capsys)
    assert code == 1
    assert line == BAD_AT + "\n"
    assert_absent(MARKER_VALUE, line)


async def test_check_and_show_write_nothing(commands: ScheduleCommands, migrated_url: URL) -> None:
    await commands.run(ScheduleRequest(action="check"))
    await commands.run(ScheduleRequest(action="show"))

    assert await fetch_all(migrated_url, "SELECT id FROM schedule_profile") == []


async def _contact_with_message(
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    url: URL,
    at: datetime,
) -> int:
    contact = await stored_contact(
        contacts,
        modes,
        ContactStatus.ACTIVE,
        tg_id=FAKE_TELEGRAM_ID,
        username=USERNAME,
        display_name=DISPLAY_NAME,
    )
    message = await messages.append(
        MessageCreate(
            contact_id=contact.id,
            direction=MessageDirection.INCOMING,
            text=TEXT,
            tg_message_id=810_001,
        )
    )
    await execute(
        url,
        f"UPDATE messages SET created_at = '{at.isoformat()}' "  # noqa: S608
        f"WHERE id = {message.id}",
    )
    return contact.id


async def test_plan_prints_the_local_time_the_reason_and_the_seconds_and_no_personal_data(
    commands: ScheduleCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    migrated_url: URL,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    contact_id = await _contact_with_message(contacts, modes, messages, migrated_url, NOON)

    code = await commands.run(ScheduleRequest(action="plan", contact_id=contact_id))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("planned for 2026-11-03 12:")
    assert f"({MOSCOW}), reason=delayed, in " in captured.out
    assert captured.out.endswith(" s\n")
    for marker in (TEXT, USERNAME, DISPLAY_NAME, FAKE_TELEGRAM_ID):
        assert_absent(marker, captured.out + captured.err + caplog.text)


async def test_plan_reports_a_postponed_reason(
    commands: ScheduleCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    migrated_url: URL,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock.now = at_local(MONDAY, 4)
    contact_id = await _contact_with_message(contacts, modes, messages, migrated_url, clock.now)

    await commands.run(ScheduleRequest(action="plan", contact_id=contact_id))

    out = capsys.readouterr().out
    assert "planned for 2026-11-02 09:" in out
    assert "reason=postponed_night" in out


async def test_plan_writes_nothing(
    commands: ScheduleCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    messages: SqlMessageRepository,
    migrated_url: URL,
) -> None:
    contact_id = await _contact_with_message(contacts, modes, messages, migrated_url, NOON)
    query = (
        "SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM scheduled_messages), "
        "(SELECT count(*) FROM schedule_profile)"
    )
    before = await fetch_all(migrated_url, query)

    await commands.run(ScheduleRequest(action="plan", contact_id=contact_id))

    assert await fetch_all(migrated_url, query) == before


async def test_plan_needs_a_contact_with_an_incoming_message(
    commands: ScheduleCommands,
    contacts: SqlContactRepository,
    modes: SqlModeRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await stored_contact(contacts, modes, ContactStatus.ACTIVE, tg_id=FAKE_TELEGRAM_ID)

    code = await commands.run(ScheduleRequest(action="plan", contact_id=contact.id))

    line = _single_error_line(capsys)
    assert code == 1
    assert "no incoming message" in line
    assert_absent(FAKE_TELEGRAM_ID, line)


async def test_a_corrupt_stored_profile_is_one_readable_line(
    commands: ScheduleCommands, migrated_url: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    await execute(
        migrated_url,
        "INSERT INTO schedule_profile (id, data) VALUES (1, '{\"version\": 9}'::jsonb)",
    )

    assert await commands.run(ScheduleRequest(action="show")) == 1

    assert "import it again" in _single_error_line(capsys)


async def test_the_import_logs_counts_only(
    commands: ScheduleCommands, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    await commands.run(_import(_file(tmp_path, VALID)))

    assert "13:00" not in caplog.text
    assert BERLIN not in caplog.text
    assert any(getattr(record, "busy_windows", None) == 1 for record in caplog.records)


@pytest.fixture
def through_main(
    migrated_url: URL, full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[URL]:
    monkeypatch.setenv("DATABASE_URL", migrated_url.render_as_string(hide_password=False))
    monkeypatch.setenv("TIMEZONE", MOSCOW)
    monkeypatch.chdir("/")
    yield migrated_url


def test_through_main_import_show_and_check(
    through_main: URL, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["schedule", "show"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == DEFAULT_LINE

    assert main(["schedule", "import", str(_file(tmp_path, VALID))]) == 0
    assert "imported" in capsys.readouterr().out

    assert main(["schedule", "show"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == STORED_LINE

    assert main(["schedule", "check", "--at", "2026-11-03 13:30"]) == 0
    assert capsys.readouterr().out == f"blocked until 2026-11-03 14:30 ({BERLIN}), kind=busy\n"

    assert main(["schedule", "check", "--at", "2026-11-03 18:00"]) == 0
    assert capsys.readouterr().out == "free\n"


def test_through_main_plan_prints_an_error_for_an_unknown_contact(
    through_main: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["schedule", "plan", "4242"]) == 1

    assert "no incoming message" in _single_error_line(capsys)


def test_through_main_a_plan_for_a_stored_message(
    through_main: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    async def prepare() -> None:
        await execute(
            through_main,
            f"INSERT INTO contacts (tg_id, status) VALUES ({FAKE_TELEGRAM_ID}, 'new')",  # noqa: S608
        )
        await execute(
            through_main,
            "INSERT INTO messages (contact_id, direction, text, tg_message_id, created_at) "  # noqa: S608
            f"VALUES (1, 'incoming', '{TEXT}', 7001, now() - interval '1 hour')",
        )

    asyncio.run(prepare())

    assert main(["schedule", "plan", "1"]) == 0

    captured = capsys.readouterr()
    assert "planned for " in captured.out
    assert "reason=" in captured.out
    for marker in (TEXT, FAKE_TELEGRAM_ID):
        assert_absent(marker, captured.out + captured.err)


@pytest.mark.parametrize(
    "arguments",
    [
        ["schedule"],
        ["schedule", "unknown"],
        ["schedule", "import"],
        ["schedule", "plan"],
        ["schedule", "plan", "not-a-number"],
        ["schedule", "check", "--at"],
        ["schedule", "show", MARKER_VALUE],
    ],
)
def test_a_bad_command_line_is_one_line_and_exit_one_and_needs_no_settings(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(arguments) == 1

    captured = capsys.readouterr()
    assert captured.err == USAGE + "\n"
    assert captured.out == ""


def test_a_bad_configuration_exits_with_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir("/")

    assert main(["schedule", "show"]) == 2
    capsys.readouterr()


def test_a_storage_failure_prints_the_class_only(
    full_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody@127.0.0.1:1/absent")
    monkeypatch.chdir("/")

    assert main(["schedule", "show"]) == 1

    assert capsys.readouterr().err.startswith("storage failed: ")
