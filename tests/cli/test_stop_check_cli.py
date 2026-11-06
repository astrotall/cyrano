from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import SqlContactRepository, SqlMessageRepository, SqlModeRepository
from cyrano.domain.enums import ContactStatus, MessageDirection
from cyrano.domain.models import Contact, MessageCreate
from cyrano.main import main
from cyrano.stop_check_cli import NOT_FOUND_LINE, USAGE, StopCheckCommands
from tests.db.conftest import fetch_all
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.reply_db import row_counts
from tests.services.outbox_fakes import stored_contact

TRIGGER_TEXT = "stopcheck-marker-zq давай встретимся"
PHOTO_TEXT = "stopcheck-photo-marker-zq скинь фото"
HARMLESS_TEXT = "stopcheck-harmless-marker-zq как дела"
OWNER_TEXT = "stopcheck-owner-marker-zq ты бот?"
USERNAME = "stopcheck_username_marker_zq"
DISPLAY_NAME = "StopcheckNameMarkerZq"
STARTED = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
MESSAGE_LINE = re.compile(r"^message [1-3]: (?:none|[a-z_]+(?:, [a-z_]+)*)$")
MARKERS = (TRIGGER_TEXT, PHOTO_TEXT, HARMLESS_TEXT, OWNER_TEXT, USERNAME, DISPLAY_NAME)


@pytest.fixture
def no_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


async def _contact(contacts: SqlContactRepository, modes: SqlModeRepository) -> Contact:
    return await stored_contact(
        contacts,
        modes,
        ContactStatus.ACTIVE,
        tg_id=FAKE_TELEGRAM_ID,
        username=USERNAME,
        display_name=DISPLAY_NAME,
    )


async def _say(
    messages: SqlMessageRepository,
    contact: Contact,
    texts: list[tuple[MessageDirection, str]],
) -> None:
    for number, (direction, text) in enumerate(texts, start=1):
        await messages.append(
            MessageCreate(
                contact_id=contact.id,
                direction=direction,
                text=text,
                tg_message_id=number,
                created_at=STARTED + timedelta(minutes=number),
            )
        )


def _assert_nothing_echoed(output: str) -> None:
    for marker in (*MARKERS, FAKE_TELEGRAM_ID):
        assert_absent(marker, output)


def test_a_text_prints_only_the_class_names_without_any_configuration(
    no_configuration: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["stop-check", "--text", TRIGGER_TEXT])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "classes: meeting\n"
    assert captured.err == ""
    _assert_nothing_echoed(captured.out + captured.err)


def test_a_text_with_several_classes_lists_them_in_order(
    no_configuration: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stop-check", "--text", f"{PHOTO_TEXT}, {OWNER_TEXT}"]) == 0

    assert capsys.readouterr().out == "classes: photo, bot_question\n"


def test_a_harmless_text_prints_none(
    no_configuration: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stop-check", f"--text={HARMLESS_TEXT}"]) == 0

    assert capsys.readouterr().out == "classes: none\n"


def test_a_text_that_starts_with_a_dash_works_with_the_equals_form(
    no_configuration: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stop-check", "--text=-скинь фото"]) == 0

    assert capsys.readouterr().out == "classes: photo\n"


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["1", "--text", TRIGGER_TEXT],
        ["1", "2"],
        ["not-a-number"],
        [TRIGGER_TEXT],
        ["--text"],
        ["--unknown", TRIGGER_TEXT],
        ["--text", TRIGGER_TEXT, "--text", PHOTO_TEXT, "extra-marker-zq"],
    ],
)
def test_bad_arguments_are_one_usage_line_and_exit_one_without_echo(
    no_configuration: None, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    code = main(["stop-check", *arguments])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"{USAGE}\n"
    _assert_nothing_echoed(captured.err)
    assert_absent("extra-marker-zq", captured.err)


async def test_a_contact_prints_the_classes_of_the_last_three_incoming_messages_newest_first(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    migrated_url: URL,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await _contact(contacts, modes)
    await _say(
        messages,
        contact,
        [
            (MessageDirection.INCOMING, OWNER_TEXT),
            (MessageDirection.INCOMING, TRIGGER_TEXT),
            (MessageDirection.OUTGOING, OWNER_TEXT),
            (MessageDirection.INCOMING, HARMLESS_TEXT),
            (MessageDirection.INCOMING, PHOTO_TEXT),
            (MessageDirection.OUTGOING, OWNER_TEXT),
        ],
    )
    before = await row_counts(migrated_url)
    contact_before = await contacts.get_by_id(contact.id)

    code = await StopCheckCommands(contacts, messages).run_contact(contact.id)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == [
        "message 1: photo",
        "message 2: none",
        "message 3: meeting",
    ]
    for line in captured.out.splitlines():
        assert MESSAGE_LINE.fullmatch(line)
    _assert_nothing_echoed(captured.out + captured.err)
    assert await row_counts(migrated_url) == before
    assert await contacts.get_by_id(contact.id) == contact_before


async def test_a_contact_without_incoming_messages_says_so(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = await _contact(contacts, modes)
    await _say(messages, contact, [(MessageDirection.OUTGOING, OWNER_TEXT)])

    code = await StopCheckCommands(contacts, messages).run_contact(contact.id)

    assert code == 0
    assert capsys.readouterr().out == "no incoming message\n"


async def test_a_missing_contact_is_a_one_line_error_with_exit_one(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await StopCheckCommands(contacts, messages).run_contact(424242)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"{NOT_FOUND_LINE}\n"
    assert "424242" not in captured.err


async def _contact_through(url: URL) -> int:
    await fetch_all(
        url,
        "INSERT INTO contacts (tg_id, status, display_name) "  # noqa: S608
        f"VALUES ({FAKE_TELEGRAM_ID}, 'active', '{DISPLAY_NAME}') RETURNING id",
    )
    [(contact_id,)] = await fetch_all(url, "SELECT id FROM contacts")
    await fetch_all(
        url,
        "INSERT INTO messages (contact_id, direction, text, tg_message_id) "  # noqa: S608
        f"VALUES ({contact_id}, 'incoming', '{TRIGGER_TEXT}', 1) RETURNING id",
    )
    return int(str(contact_id))


def test_through_main_a_contact_is_checked_without_writes(
    migrated_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    contact_id = asyncio.run(_contact_through(migrated_url))
    before = asyncio.run(row_counts(migrated_url))

    code = main(["stop-check", str(contact_id)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "message 1: meeting\n"
    _assert_nothing_echoed(captured.out + captured.err)
    assert asyncio.run(row_counts(migrated_url)) == before
    assert asyncio.run(fetch_all(migrated_url, "SELECT status, pause_reason FROM contacts")) == [
        ("active", None)
    ]


def test_through_main_a_missing_contact_exits_one(
    migrated_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["stop-check", "999999"]) == 1
    assert capsys.readouterr().err == f"{NOT_FOUND_LINE}\n"


def test_an_unreachable_database_is_a_readable_error(
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:x@127.0.0.1:1/none")

    code = main(["stop-check", "1"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("storage failed: ")


def test_a_contact_check_without_configuration_exits_with_the_usual_code(
    no_configuration: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stop-check", "1"]) == 2
    assert "Missing required environment variables" in capsys.readouterr().err
