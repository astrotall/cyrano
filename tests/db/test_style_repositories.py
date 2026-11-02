from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from cyrano.db.repositories import SqlPersonaRepository, SqlStyleExampleRepository
from cyrano.domain.enums import TurnRole
from cyrano.domain.style import StyleExampleCreate, Turn
from cyrano.services.repositories import PersonaRepository, StyleExampleRepository
from tests.db.conftest import execute, fetch_all


def make_example(
    reply: str, *, tags: tuple[str, ...] = (), question: str = "question"
) -> StyleExampleCreate:
    return StyleExampleCreate.model_validate(
        {
            "tags": list(tags),
            "turns": [
                {"role": "them", "text": question},
                {"role": "me", "text": reply},
            ],
        }
    )


def test_repositories_satisfy_the_service_protocols(
    personas: SqlPersonaRepository, style_examples: SqlStyleExampleRepository
) -> None:
    persona: PersonaRepository = personas
    examples: StyleExampleRepository = style_examples

    assert persona is not None
    assert examples is not None


async def test_there_is_no_persona_until_one_is_stored(personas: SqlPersonaRepository) -> None:
    assert await personas.get() is None


async def test_replacing_the_persona_stores_and_returns_it(
    personas: SqlPersonaRepository,
) -> None:
    stored = await personas.replace("first text")

    assert stored.text == "first text"
    assert stored.updated_at.utcoffset() is not None
    assert await personas.get() == stored


async def test_replacing_again_updates_text_and_time_in_the_single_row(
    personas: SqlPersonaRepository, migrated_url: URL
) -> None:
    first = await personas.replace("first text")

    second = await personas.replace("second text")

    assert second.text == "second text"
    assert second.updated_at > first.updated_at
    assert await fetch_all(migrated_url, "SELECT id, text FROM persona") == [(1, "second text")]


async def test_the_database_refuses_a_second_persona_row(migrated_url: URL) -> None:
    with pytest.raises(DBAPIError):
        await execute(migrated_url, "INSERT INTO persona (id, text) VALUES (2, 'other')")


async def test_adding_examples_counts_added_and_skipped(
    style_examples: SqlStyleExampleRepository,
) -> None:
    first = await style_examples.add_many([make_example("a"), make_example("b")])
    again = await style_examples.add_many([make_example("b"), make_example("c")])

    assert (first.added, first.skipped) == (2, 0)
    assert (again.added, again.skipped) == (1, 1)
    assert await style_examples.count() == 3


async def test_duplicates_inside_one_batch_are_skipped(
    style_examples: SqlStyleExampleRepository,
) -> None:
    result = await style_examples.add_many([make_example("a"), make_example("a")])

    assert (result.added, result.skipped) == (1, 1)


async def test_adding_nothing_is_a_no_op(style_examples: SqlStyleExampleRepository) -> None:
    result = await style_examples.add_many([])

    assert (result.added, result.skipped) == (0, 0)
    assert await style_examples.count() == 0


async def test_tags_are_part_of_the_identity(style_examples: SqlStyleExampleRepository) -> None:
    result = await style_examples.add_many(
        [make_example("a", tags=("x",)), make_example("a", tags=("y",)), make_example("a")]
    )

    assert result.added == 3


async def test_examples_round_trip_with_roles_and_utc_times(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await style_examples.add_many([make_example("answer", tags=("b", "a"))])

    [stored] = await style_examples.list_all()

    assert stored.tags == ["a", "b"]
    assert stored.turns == [
        Turn(role=TurnRole.THEM, text="question"),
        Turn(role=TurnRole.ME, text="answer"),
    ]
    assert stored.created_at.utcoffset() == timedelta(0)


async def test_listing_is_newest_first_and_ties_break_by_id(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await style_examples.add_many([make_example("old")])
    await style_examples.add_many([make_example("new-a"), make_example("new-b")])

    replies = [example.turns[-1].text for example in await style_examples.list_all()]

    assert replies == ["new-b", "new-a", "old"]


async def test_delete_all_returns_the_removed_count(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await style_examples.add_many([make_example("a"), make_example("b")])

    assert await style_examples.delete_all() == 2
    assert await style_examples.count() == 0
    assert await style_examples.delete_all() == 0


async def test_replace_swaps_everything(style_examples: SqlStyleExampleRepository) -> None:
    await style_examples.add_many([make_example("old-1"), make_example("old-2")])

    result = await style_examples.add_many(
        [make_example("old-2"), make_example("new")], replace=True
    )

    assert (result.added, result.skipped) == (2, 0)
    replies = {example.turns[-1].text for example in await style_examples.list_all()}
    assert replies == {"old-2", "new"}


async def test_a_failed_replace_leaves_the_old_examples_intact(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await style_examples.add_many([make_example("keep-1"), make_example("keep-2")])
    poisoned = StyleExampleCreate.model_construct(
        tags=[], turns=[Turn.model_construct(role=TurnRole.ME, text="bad\x00text")]
    )

    with pytest.raises(DBAPIError):
        await style_examples.add_many([make_example("new"), poisoned], replace=True)

    replies = {example.turns[-1].text for example in await style_examples.list_all()}
    assert replies == {"keep-1", "keep-2"}


async def test_the_content_hash_is_unique_in_the_database(
    style_examples: SqlStyleExampleRepository, migrated_url: URL
) -> None:
    await style_examples.add_many([make_example("a")])

    with pytest.raises(DBAPIError):
        await execute(
            migrated_url,
            "INSERT INTO style_examples (tags, turns, content_hash) "
            "SELECT tags, turns, content_hash FROM style_examples",
        )
