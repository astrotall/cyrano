from __future__ import annotations

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlPersonaRepository,
    SqlStyleExampleRepository,
)
from cyrano.domain.enums import ContactGender, ContactStatus, MessageDirection
from cyrano.domain.models import Contact, ContactCreate, MessageCreate
from cyrano.domain.style import StyleExampleCreate
from cyrano.services.gender import GenderResolver
from cyrano.services.llm_client import LLMClient
from cyrano.services.persona_service import PersonaService
from cyrano.services.reply_generator import ReplyGenerator
from cyrano.services.style_examples import ExampleSelector
from tests.db.conftest import fetch_all
from tests.privacy import FAKE_TELEGRAM_ID
from tests.reply_data import make_builder

PERSONA_MARKER = "persona-marker-zq lives in an invented town"
MESSAGE_MARKER = "message-marker-zq"
REPLY_MARKER = "reply-marker-zq"
EXAMPLE_MARKER = "example-marker-zq"
BIO_MARKER = "bio-marker-zq"
USERNAME_MARKER = "username_marker_zq"
HISTORY_LIMIT = 20
COUNT_QUERIES = (
    "SELECT count(*) FROM contacts",
    "SELECT count(*) FROM messages",
    "SELECT count(*) FROM modes",
    "SELECT count(*) FROM persona",
    "SELECT count(*) FROM style_examples",
)


class Repositories:
    def __init__(
        self,
        contacts: SqlContactRepository,
        messages: SqlMessageRepository,
        modes: SqlModeRepository,
        personas: SqlPersonaRepository,
        style_examples: SqlStyleExampleRepository,
    ) -> None:
        self.contacts = contacts
        self.messages = messages
        self.modes = modes
        self.personas = personas
        self.style_examples = style_examples

    def generator(
        self,
        client: LLMClient,
        *,
        history_limit: int = HISTORY_LIMIT,
        budget: int = 1_000_000,
        default_gender: ContactGender | None = None,
    ) -> ReplyGenerator:
        return ReplyGenerator(
            contacts=self.contacts,
            messages=self.messages,
            modes=self.modes,
            persona=PersonaService(self.personas),
            examples=ExampleSelector(self.style_examples, 8),
            gender=GenderResolver(default_gender),
            builder=make_builder(budget),
            client=client,
            history_limit=history_limit,
        )

    async def contact(self, *, tg_id: int = FAKE_TELEGRAM_ID, mode: str | None = None) -> Contact:
        created = await self.contacts.create(
            ContactCreate(
                tg_id=tg_id,
                username=USERNAME_MARKER,
                display_name="Contactfirst",
                profile={"bio": BIO_MARKER},
            )
        )
        if mode is None:
            return created
        stored = await self.modes.get_by_name(mode)
        assert stored is not None
        await self.contacts.set_status(created.id, ContactStatus.PENDING)
        return await self.contacts.set_status(created.id, ContactStatus.ACTIVE, mode_id=stored.id)

    async def say(
        self, contact_id: int, direction: MessageDirection, text: str, number: int
    ) -> None:
        await self.messages.append(
            MessageCreate(
                contact_id=contact_id, direction=direction, text=text, tg_message_id=number
            )
        )

    async def add_examples(self, count: int) -> None:
        for number in range(1, count + 1):
            await self.style_examples.add_many(
                [
                    StyleExampleCreate.model_validate(
                        {
                            "turns": [
                                {"role": "them", "text": f"{EXAMPLE_MARKER} question {number}"},
                                {"role": "me", "text": f"{EXAMPLE_MARKER} answer {number}"},
                            ]
                        }
                    )
                ]
            )

    async def conversation(self, contact_id: int, count: int = 3) -> None:
        for number in range(1, count + 1):
            direction = MessageDirection.INCOMING if number % 2 else MessageDirection.OUTGOING
            await self.say(contact_id, direction, f"{MESSAGE_MARKER} {number}", number)


@pytest.fixture
def repos(
    contacts: SqlContactRepository,
    messages: SqlMessageRepository,
    modes: SqlModeRepository,
    personas: SqlPersonaRepository,
    style_examples: SqlStyleExampleRepository,
) -> Repositories:
    return Repositories(contacts, messages, modes, personas, style_examples)


async def row_counts(url: URL) -> list[object]:
    counts: list[object] = []
    for query in COUNT_QUERIES:
        rows = await fetch_all(url, query)
        counts.append(rows[0][0])
    return counts


async def ready_contact(repos: Repositories, *, mode: str | None = None) -> Contact:
    await repos.personas.replace(PERSONA_MARKER)
    contact = await repos.contact(mode=mode)
    await repos.conversation(contact.id)
    return contact
