from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from cyrano.contact_cli import ContactCommands, ContactRequest
from cyrano.db.repositories import SqlContactRepository, SqlMessageRepository, SqlModeRepository
from cyrano.domain.enums import ContactGender, ContactStatus
from cyrano.domain.models import ContactCreate
from cyrano.logging import JsonFormatter
from cyrano.services.card_actions import CardActions
from cyrano.services.gender import GenderResolver
from cyrano.services.gender_actions import GenderActions
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.reply_db import PERSONA_MARKER, Repositories, ready_contact
from tests.services.fakes import ScriptedLLMClient

NAME_MARKER = "Gender-Privacy-Name-zq"
GENDER_WORDS = ("female", "male", "девушка", "парень", "Девушка", "Парень")


@contextmanager
def _captured_json_logs() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _assert_clean(logged: str) -> None:
    assert logged
    assert_absent(NAME_MARKER, logged)
    assert_absent(FAKE_TELEGRAM_ID, logged)
    for word in GENDER_WORDS:
        assert word not in logged


async def test_the_gender_paths_never_log_a_gender_next_to_a_name(
    contacts: SqlContactRepository, messages: SqlMessageRepository, modes: SqlModeRepository
) -> None:
    default = await modes.get_by_name("default")
    assert default is not None
    resolver = GenderResolver(ContactGender.MALE)
    created = await contacts.create(ContactCreate(tg_id=FAKE_TELEGRAM_ID, display_name=NAME_MARKER))
    await contacts.set_status(created.id, ContactStatus.PENDING)
    gender_actions = GenderActions(contacts, messages, modes, resolver)
    card_actions = CardActions(contacts, modes, resolver)

    with _captured_json_logs() as stream:
        logging.getLogger("probe").info("gender probe")
        resolver.resolve(created)
        await gender_actions.set_gender(created.id, ContactGender.FEMALE)
        await gender_actions.set_gender(created.id, ContactGender.FEMALE)
        await gender_actions.set_gender(424242, ContactGender.MALE)
        await card_actions.reply(created.id, default.id)
        await ContactCommands(contacts, resolver).run(ContactRequest("gender", created.id, None))

    _assert_clean(stream.getvalue())


async def test_the_reply_generator_logs_no_gender_and_no_name(repos: Repositories) -> None:
    contact = await ready_contact(repos)
    await repos.contacts.update_identity(contact.id, None, NAME_MARKER)
    await repos.contacts.set_gender(contact.id, ContactGender.FEMALE)

    with _captured_json_logs() as stream:
        logging.getLogger("probe").info("gender probe")
        await repos.generator(
            ScriptedLLMClient("fine"), default_gender=ContactGender.MALE
        ).generate(contact.id)

    logged = stream.getvalue()
    _assert_clean(logged)
    assert_absent(PERSONA_MARKER, logged)


async def test_the_command_prints_only_the_gender_and_its_source(
    contacts: SqlContactRepository, capsys: pytest.CaptureFixture[str]
) -> None:
    created = await contacts.create(ContactCreate(tg_id=FAKE_TELEGRAM_ID, display_name=NAME_MARKER))

    await ContactCommands(contacts, GenderResolver()).run(
        ContactRequest("gender", created.id, ContactGender.FEMALE)
    )

    out = capsys.readouterr()
    assert out.out == "gender: female (owner)\n"
    assert_absent(NAME_MARKER, out.out + out.err)
    assert_absent(FAKE_TELEGRAM_ID, out.out + out.err)
