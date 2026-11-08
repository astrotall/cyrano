from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from cyrano.contact_cli import USAGE, ContactCommands, ContactRequest
from cyrano.db.repositories import SqlContactRepository
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactGender
from cyrano.domain.models import ContactCreate
from cyrano.main import main
from cyrano.services.gender import GenderResolver
from tests.db.conftest import fetch_all
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent

NAME_MARKER = "Contact-Cli-Name-zq"
FEMALE = ContactGender.FEMALE
MALE = ContactGender.MALE


async def _contact(session_factory: SessionFactory, name: str | None = NAME_MARKER) -> int:
    contacts = SqlContactRepository(session_factory)
    created = await contacts.create(ContactCreate(tg_id=FAKE_TELEGRAM_ID, display_name=name))
    return created.id


def _commands(
    session_factory: SessionFactory, default: ContactGender | None = None
) -> ContactCommands:
    return ContactCommands(SqlContactRepository(session_factory), GenderResolver(default))


async def _stored(session_factory: SessionFactory, contact_id: int) -> ContactGender | None:
    contact = await SqlContactRepository(session_factory).get_by_id(contact_id)
    assert contact is not None
    return contact.gender


@pytest.mark.parametrize(
    ("chosen", "line"),
    [(FEMALE, "gender: female (owner)\n"), (MALE, "gender: male (owner)\n")],
)
async def test_a_choice_is_stored_and_printed_with_its_source(
    session_factory: SessionFactory,
    capsys: pytest.CaptureFixture[str],
    chosen: ContactGender,
    line: str,
) -> None:
    contact_id = await _contact(session_factory)

    code = await _commands(session_factory).run(ContactRequest("gender", contact_id, chosen))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == line
    assert captured.err == ""
    assert await _stored(session_factory, contact_id) is chosen
    assert_absent(NAME_MARKER, captured.out)


async def test_unknown_clears_the_choice_and_prints_the_remaining_source(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _contact(session_factory, name="Анна")
    commands = _commands(session_factory)
    await commands.run(ContactRequest("gender", contact_id, MALE))
    capsys.readouterr()

    code = await commands.run(ContactRequest("gender", contact_id, None))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "gender: female (name)\n"
    assert await _stored(session_factory, contact_id) is None


async def test_clearing_with_nothing_else_known_prints_unknown(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _contact(session_factory, name=None)
    commands = _commands(session_factory)
    await commands.run(ContactRequest("gender", contact_id, FEMALE))
    capsys.readouterr()

    code = await commands.run(ContactRequest("gender", contact_id, None))

    assert code == 0
    assert capsys.readouterr().out == "gender: unknown (unknown)\n"
    assert await _stored(session_factory, contact_id) is None


async def test_clearing_falls_back_to_the_default_setting(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _contact(session_factory, name=None)
    commands = _commands(session_factory, MALE)
    await commands.run(ContactRequest("gender", contact_id, FEMALE))
    capsys.readouterr()

    await commands.run(ContactRequest("gender", contact_id, None))

    assert capsys.readouterr().out == "gender: male (default)\n"


async def test_the_same_choice_twice_is_stored_once_and_printed_twice(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact_id = await _contact(session_factory)
    commands = _commands(session_factory)

    first = await commands.run(ContactRequest("gender", contact_id, FEMALE))
    second = await commands.run(ContactRequest("gender", contact_id, FEMALE))

    assert (first, second) == (0, 0)
    assert capsys.readouterr().out == "gender: female (owner)\n" * 2


async def test_an_unknown_contact_is_a_one_line_message_and_exit_one(
    session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _commands(session_factory).run(ContactRequest("gender", 424242, FEMALE))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == "contact gender failed: no contact with this id\n"


@pytest.fixture
def through_main(migrated_url: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> URL:
    monkeypatch.chdir(tmp_path)
    return migrated_url


def _insert_contact(url: URL) -> int:
    asyncio.run(
        fetch_all(
            url,
            "INSERT INTO contacts (tg_id, status, display_name) "  # noqa: S608
            f"VALUES ({FAKE_TELEGRAM_ID}, 'new', '{NAME_MARKER}') RETURNING id",
        )
    )
    [(contact_id,)] = asyncio.run(fetch_all(url, "SELECT id FROM contacts"))
    return int(str(contact_id))


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ("female", "gender: female (owner)\n"),
        ("male", "gender: male (owner)\n"),
        ("unknown", "gender: unknown (unknown)\n"),
    ],
)
def test_through_main_each_value_works_on_the_database(
    through_main: URL, capsys: pytest.CaptureFixture[str], value: str, line: str
) -> None:
    contact_id = _insert_contact(through_main)

    code = main(["contact", "gender", str(contact_id), value])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == line
    assert captured.err == ""
    stored = asyncio.run(fetch_all(through_main, "SELECT gender FROM contacts"))
    assert stored == [(None if value == "unknown" else value,)]


def test_through_main_an_unknown_id_exits_one(
    through_main: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["contact", "gender", "424242", "female"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == "contact gender failed: no contact with this id\n"


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["gender"],
        ["gender", "3"],
        ["gender", "x", "female"],
        ["gender", "3", "other"],
        ["gender", "3", "Female"],
        ["gender", "3", "female", "extra"],
        ["gender", "3", "--yes"],
        ["remove", "3", "female"],
    ],
)
def test_a_bad_command_line_prints_the_usage_and_exits_with_one(
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(["contact", *arguments])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == f"{USAGE}\n"


def test_a_storage_failure_prints_the_class_only(
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:x@127.0.0.1:1/none")

    code = main(["contact", "gender", "3", "female"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("storage failed: ")


def test_a_missing_configuration_exits_with_the_usual_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["contact", "gender", "3", "female"]) == 2


def test_the_default_setting_is_used_by_the_command(
    through_main: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    full_env: dict[str, str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", through_main.render_as_string(hide_password=False))
    monkeypatch.setenv("DEFAULT_CONTACT_GENDER", "female")
    asyncio.run(
        fetch_all(
            through_main,
            "INSERT INTO contacts (tg_id, status) "  # noqa: S608
            f"VALUES ({FAKE_TELEGRAM_ID}, 'new') RETURNING id",
        )
    )
    [(contact_id,)] = asyncio.run(fetch_all(through_main, "SELECT id FROM contacts"))

    code = main(["contact", "gender", str(contact_id), "unknown"])

    assert code == 0
    assert capsys.readouterr().out == "gender: female (default)\n"
