from __future__ import annotations

from cyrano.db.repositories import SqlStyleExampleRepository
from cyrano.domain.style import StyleExample, StyleExampleCreate
from cyrano.services.style_examples import ExampleSelector, collect_example_stats

DEFAULT_LIMIT = 8


def make_example(reply: str, *tags: str) -> StyleExampleCreate:
    return StyleExampleCreate.model_validate(
        {
            "tags": list(tags),
            "turns": [
                {"role": "them", "text": "question"},
                {"role": "me", "text": reply},
            ],
        }
    )


def replies(examples: list[StyleExample]) -> list[str]:
    return [example.turns[-1].text for example in examples]


async def store_in_order(repository: SqlStyleExampleRepository, *items: StyleExampleCreate) -> None:
    for item in items:
        await repository.add_many([item])


async def test_an_empty_store_returns_nothing(style_examples: SqlStyleExampleRepository) -> None:
    selector = ExampleSelector(style_examples, DEFAULT_LIMIT)

    assert await selector.select("flirty") == []


async def test_mode_tagged_examples_come_before_generic_ones(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await store_in_order(
        style_examples,
        make_example("generic-old"),
        make_example("flirty-old", "flirty"),
        make_example("generic-new"),
        make_example("flirty-new", "flirty"),
    )

    selected = await ExampleSelector(style_examples, DEFAULT_LIMIT).select("flirty")

    assert replies(selected) == ["flirty-new", "flirty-old", "generic-new", "generic-old"]


async def test_an_example_tagged_only_for_other_modes_is_never_selected(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await store_in_order(
        style_examples,
        make_example("other-only", "serious"),
        make_example("other-two", "serious", "calm"),
        make_example("generic"),
        make_example("mine", "flirty"),
    )

    selected = await ExampleSelector(style_examples, DEFAULT_LIMIT).select("flirty")

    assert replies(selected) == ["mine", "generic"]


async def test_an_example_with_several_tags_matches_any_of_them(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await store_in_order(style_examples, make_example("shared", "default", "flirty"))
    selector = ExampleSelector(style_examples, DEFAULT_LIMIT)

    assert replies(await selector.select("flirty")) == ["shared"]
    assert replies(await selector.select("default")) == ["shared"]
    assert await selector.select("serious") == []


async def test_a_mode_without_tagged_examples_gets_only_generic_ones(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await store_in_order(style_examples, make_example("g1"), make_example("t", "flirty"))

    selected = await ExampleSelector(style_examples, DEFAULT_LIMIT).select("unknown-mode")

    assert replies(selected) == ["g1"]


async def test_the_mode_name_is_matched_ignoring_case_and_surrounding_spaces(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await store_in_order(style_examples, make_example("mine", "Flirty"))
    selector = ExampleSelector(style_examples, DEFAULT_LIMIT)

    assert replies(await selector.select("FLIRTY")) == ["mine"]
    assert replies(await selector.select("  flirty ")) == ["mine"]


async def test_the_limit_is_respected(style_examples: SqlStyleExampleRepository) -> None:
    await store_in_order(
        style_examples,
        make_example("g"),
        make_example("t1", "flirty"),
        make_example("t2", "flirty"),
    )
    selector = ExampleSelector(style_examples, DEFAULT_LIMIT)

    assert replies(await selector.select("flirty", 2)) == ["t2", "t1"]
    assert replies(await selector.select("flirty", 3)) == ["t2", "t1", "g"]
    assert replies(await selector.select("flirty", 1)) == ["t2"]


async def test_a_limit_larger_than_what_is_available_returns_everything_eligible(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await store_in_order(style_examples, make_example("g"), make_example("other", "serious"))

    selected = await ExampleSelector(style_examples, DEFAULT_LIMIT).select("flirty", 50)

    assert replies(selected) == ["g"]


async def test_a_limit_of_zero_or_less_returns_nothing(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await store_in_order(style_examples, make_example("g"))
    selector = ExampleSelector(style_examples, DEFAULT_LIMIT)

    assert await selector.select("flirty", 0) == []
    assert await selector.select("flirty", -3) == []


async def test_the_configured_limit_applies_when_none_is_given(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await store_in_order(style_examples, *[make_example(f"g{index}") for index in range(5)])

    assert len(await ExampleSelector(style_examples, 3).select("flirty")) == 3
    assert await ExampleSelector(style_examples, 0).select("flirty") == []


async def test_the_order_is_deterministic_when_creation_times_are_equal(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await style_examples.add_many(
        [
            make_example("first", "flirty"),
            make_example("second", "flirty"),
            make_example("third", "flirty"),
            make_example("g-first"),
            make_example("g-second"),
        ]
    )
    stored = await style_examples.list_all()
    assert len({example.created_at for example in stored}) == 1
    selector = ExampleSelector(style_examples, DEFAULT_LIMIT)

    runs = [replies(await selector.select("flirty")) for _ in range(3)]

    assert runs[0] == ["third", "second", "first", "g-second", "g-first"]
    assert runs[1] == runs[0] == runs[2]


async def test_stats_count_generic_examples_and_every_tag(
    style_examples: SqlStyleExampleRepository,
) -> None:
    await style_examples.add_many(
        [
            make_example("a"),
            make_example("b", "default"),
            make_example("c", "default", "flirty"),
            make_example("d", "flirty"),
            make_example("e", "flirty"),
        ]
    )

    stats = collect_example_stats(await style_examples.list_all())

    assert stats.total == 5
    assert stats.generic == 1
    assert stats.by_tag == {"default": 2, "flirty": 3}


def test_stats_of_nothing_are_zero() -> None:
    stats = collect_example_stats([])

    assert (stats.total, stats.generic, stats.by_tag) == (0, 0, {})
