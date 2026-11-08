from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cyrano.db.repositories import SqlSummaryRepository
from cyrano.db.session import SessionFactory
from cyrano.domain.models import Contact, ContactUpdate
from cyrano.domain.summary import SUMMARY_VERSION, ContactSummary, StoredSummary, SummaryCursor
from cyrano.prompts.texts import (
    SUMMARY_HEADER,
    SUMMARY_MEMORY_NOTE,
    TOLD_ABOUT_OWNER_NOTE,
)
from tests.reply_db import Repositories, ready_contact
from tests.services.fakes import ScriptedLLMClient

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
TOLD_MARKER = "told-marker-zq"
NOTE_MARKER = "note-marker-zq"
STORED = ContactSummary(
    name="Оля", interests=["велосипед"], notes=[NOTE_MARKER], told_about_owner=[TOLD_MARKER]
)


async def _store(
    session_factory: SessionFactory, contact: Contact, data: ContactSummary = STORED
) -> None:
    stored = StoredSummary(
        version=SUMMARY_VERSION,
        updated_at=NOW,
        cursor=SummaryCursor(created_at=NOW, message_id=1),
        data=data,
    )
    assert await SqlSummaryRepository(session_factory).store_summary(contact.id, stored, None)


async def _system(repos: Repositories, contact_id: int, *, budget: int = 1_000_000) -> str:
    prompt = await repos.generator(ScriptedLLMClient(), budget=budget).prepare(contact_id)
    return prompt.messages[0].content


async def test_the_stored_summary_is_the_layer_with_the_note_and_the_told_block(
    repos: Repositories, session_factory: SessionFactory
) -> None:
    contact = await ready_contact(repos)
    await _store(session_factory, contact)
    client = ScriptedLLMClient("fine")

    await repos.generator(client).generate(contact.id)

    system = client.calls[0][0][0].content
    layer = system.split(f"{SUMMARY_HEADER}\n")[1].split("\n\n")[0]
    assert layer.splitlines() == [
        SUMMARY_MEMORY_NOTE,
        "- Имя: Оля",
        "- Интересы: велосипед",
        f"- Заметки: {NOTE_MARKER}",
        TOLD_ABOUT_OWNER_NOTE,
        f"- {TOLD_MARKER}",
    ]


async def test_no_summary_means_no_layer_and_the_same_prompt_as_before(
    repos: Repositories, session_factory: SessionFactory
) -> None:
    contact = await ready_contact(repos)
    before = await _system(repos, contact.id)
    await _store(session_factory, contact)
    with_summary = await _system(repos, contact.id)
    await SqlSummaryRepository(session_factory).clear_summary(contact.id)

    after = await _system(repos, contact.id)

    assert SUMMARY_HEADER not in before
    assert SUMMARY_HEADER in with_summary
    assert after == before


@pytest.mark.parametrize(
    "raw",
    [
        {"topic": "old shape"},
        {"version": 1, "cursor": "broken", "data": {"name": "x"}},
        {"version": 7, "updated_at": NOW.isoformat(), "cursor": {}, "data": {}},
    ],
)
async def test_an_unreadable_stored_summary_is_ignored(
    repos: Repositories, raw: dict[str, object]
) -> None:
    contact = await ready_contact(repos)
    await repos.contacts.update(contact.id, ContactUpdate(summary=raw))

    system = await _system(repos, contact.id)

    assert SUMMARY_HEADER not in system


async def test_an_empty_stored_summary_gives_no_layer(
    repos: Repositories, session_factory: SessionFactory
) -> None:
    contact = await ready_contact(repos)
    await _store(session_factory, contact, ContactSummary())

    assert SUMMARY_HEADER not in await _system(repos, contact.id)


async def test_the_layer_is_never_trimmed_when_the_budget_trims_examples_and_history(
    repos: Repositories, session_factory: SessionFactory
) -> None:
    contact = await ready_contact(repos)
    await repos.add_examples(6)
    await _store(session_factory, contact)
    full = await repos.generator(ScriptedLLMClient()).prepare(contact.id)
    budget = full.characters - 200

    trimmed = await repos.generator(ScriptedLLMClient(), budget=budget).prepare(contact.id)

    assert trimmed.examples_kept < trimmed.examples_offered
    system = trimmed.messages[0].content
    assert TOLD_MARKER in system
    assert NOTE_MARKER in system
    assert SUMMARY_MEMORY_NOTE in system


async def test_an_explicit_summary_wins_over_the_stored_one(
    repos: Repositories, session_factory: SessionFactory
) -> None:
    contact = await ready_contact(repos)
    await _store(session_factory, contact)
    client = ScriptedLLMClient("fine")

    await repos.generator(client).generate(contact.id, summary="explicit-summary-zq")

    system = client.calls[0][0][0].content
    assert "explicit-summary-zq" in system
    assert TOLD_MARKER not in system
