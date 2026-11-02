"""Selection and statistics of the stored style examples."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from cyrano.domain.style import StyleExample
from cyrano.services.repositories import StyleExampleRepository


@dataclass(frozen=True)
class ExampleStats:
    total: int
    generic: int
    by_tag: dict[str, int]


def collect_example_stats(examples: Sequence[StyleExample]) -> ExampleStats:
    by_tag = Counter(tag for example in examples for tag in example.tags)
    return ExampleStats(
        total=len(examples),
        generic=sum(1 for example in examples if not example.tags),
        by_tag=dict(sorted(by_tag.items())),
    )


class ExampleSelector:
    def __init__(self, repository: StyleExampleRepository, default_limit: int) -> None:
        self._repository = repository
        self._default_limit = default_limit

    async def select(self, mode_name: str, limit: int | None = None) -> list[StyleExample]:
        effective = self._default_limit if limit is None else limit
        if effective <= 0:
            return []
        mode = mode_name.strip().lower()
        examples = await self._repository.list_all()
        tagged = [example for example in examples if mode in example.tags]
        generic = [example for example in examples if not example.tags]
        return [*tagged, *generic][:effective]
