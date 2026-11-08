from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel

from cyrano.domain.errors import LLMError
from cyrano.domain.llm import LLMMessage, LLMResult
from cyrano.domain.models import Message
from cyrano.domain.summary import StoredSummary, SummaryCursor, SummaryDue


class InMemorySummaries:
    def __init__(self, due: Sequence[SummaryDue] = (), *, error: Exception | None = None) -> None:
        self.due = list(due)
        self.error = error
        self.due_calls: list[tuple[int, timedelta, datetime, int]] = []
        self.failed: list[tuple[int, datetime]] = []
        self.cleared: list[int] = []

    async def messages_after(
        self, contact_id: int, cursor: SummaryCursor | None, limit: int
    ) -> list[Message]:
        return []

    async def count_after(self, contact_id: int, cursor: SummaryCursor | None) -> int:
        return 0

    async def store_summary(
        self, contact_id: int, stored: StoredSummary, expected_cursor: SummaryCursor | None
    ) -> bool:
        return False

    async def mark_summary_failed(self, contact_id: int, at: datetime) -> None:
        self.failed.append((contact_id, at))

    async def clear_summary(self, contact_id: int) -> bool:
        self.cleared.append(contact_id)
        return False

    async def contacts_due_for_summary(
        self, *, every: int, cooldown: timedelta, now: datetime, limit: int
    ) -> list[SummaryDue]:
        self.due_calls.append((every, cooldown, now, limit))
        if self.error is not None:
            raise self.error
        return list(self.due)


class RecordingSummaryQueue:
    def __init__(self, *, failing_ids: frozenset[int] = frozenset()) -> None:
        self.jobs: list[tuple[int, int]] = []
        self._failing_ids = failing_ids

    async def enqueue(self, contact_id: int, up_to_message_id: int) -> None:
        if contact_id in self._failing_ids:
            raise ConnectionError("invented queue failure")
        self.jobs.append((contact_id, up_to_message_id))


type JsonStep = str | BaseModel | LLMError | Exception
type BeforeReply = Callable[[], Awaitable[None]]


class ScriptedJsonClient:
    def __init__(self, *steps: JsonStep, before_reply: BeforeReply | None = None) -> None:
        self._steps = list(steps)
        self._before_reply = before_reply
        self.calls: list[tuple[Sequence[LLMMessage], type[BaseModel]]] = []

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        raise NotImplementedError

    async def complete_json[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        schema: type[T],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        self.calls.append((messages, schema))
        if self._before_reply is not None:
            await self._before_reply()
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, str):
            return schema.model_validate_json(step)
        return schema.model_validate(step.model_dump())

    def system(self, call: int = 0) -> str:
        return self.calls[call][0][0].content

    def user(self, call: int = 0) -> str:
        return self.calls[call][0][1].content
