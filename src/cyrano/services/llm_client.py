from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from cyrano.domain.llm import LLMMessage, LLMResult


class LLMClient(Protocol):
    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult: ...

    async def complete_json[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T: ...
