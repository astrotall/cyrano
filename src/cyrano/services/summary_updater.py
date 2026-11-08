from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime

from pydantic import ValidationError

from cyrano.domain.enums import Role, SummaryOutcome, SummaryReason
from cyrano.domain.errors import ContactNotFound, LLMError, SummaryPromptTooLarge
from cyrano.domain.llm import LLMMessage
from cyrano.domain.models import Message
from cyrano.domain.summary import (
    SUMMARY_CHUNK_MESSAGES,
    SUMMARY_ELIGIBLE_STATUSES,
    SUMMARY_PROMPT_MAX_CHARS,
    SUMMARY_VERSION,
    ContactSummary,
    StoredSummary,
    SummaryCursor,
    SummaryPrompt,
    SummaryUpdateResult,
    read_stored_summary,
)
from cyrano.prompts.render import render_summary_system_prompt, render_summary_user_prompt
from cyrano.services.llm_client import LLMClient
from cyrano.services.repositories import ContactRepository, SummaryRepository
from cyrano.services.summary_sanitizer import sanitize_summary

logger = logging.getLogger("cyrano.services.summary_updater")


def build_summary_prompt(
    previous: ContactSummary | None, messages: Sequence[Message], *, max_chars: int
) -> SummaryPrompt:
    system = render_summary_system_prompt()
    characters = len(system)
    for kept in range(len(messages), 0, -1):
        user = render_summary_user_prompt(previous=previous, messages=messages[-kept:])
        characters = len(system) + len(user)
        if characters <= max_chars:
            return SummaryPrompt(
                messages=(
                    LLMMessage(role=Role.SYSTEM, content=system),
                    LLMMessage(role=Role.USER, content=user),
                ),
                messages_offered=len(messages),
                messages_kept=kept,
                characters=characters,
            )
    raise SummaryPromptTooLarge(characters, max_chars)


def _cursor_of(message: Message) -> SummaryCursor:
    return SummaryCursor(created_at=message.created_at, message_id=message.id)


class SummaryUpdater:
    def __init__(
        self,
        *,
        contacts: ContactRepository,
        summaries: SummaryRepository,
        client: LLMClient,
        clock: Callable[[], datetime],
        chunk_messages: int = SUMMARY_CHUNK_MESSAGES,
        prompt_max_chars: int = SUMMARY_PROMPT_MAX_CHARS,
    ) -> None:
        self._contacts = contacts
        self._summaries = summaries
        self._client = client
        self._clock = clock
        self._chunk_messages = chunk_messages
        self._prompt_max_chars = prompt_max_chars

    async def update(self, contact_id: int) -> SummaryUpdateResult:
        try:
            result = await self._update(contact_id)
        except Exception as exc:
            result = await self._failed(contact_id, SummaryReason.UNEXPECTED_ERROR, exc)
        _log(contact_id, result)
        return result

    async def _update(self, contact_id: int) -> SummaryUpdateResult:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return _result(SummaryOutcome.CONTACT_MISSING, SummaryReason.CONTACT_MISSING)
        if contact.status not in SUMMARY_ELIGIBLE_STATUSES:
            return _result(SummaryOutcome.NOT_ELIGIBLE, SummaryReason.STATUS_NOT_ELIGIBLE)
        previous = read_stored_summary(contact.summary)
        cursor = None if previous is None else previous.cursor
        chunk = await self._summaries.messages_after(contact_id, cursor, self._chunk_messages)
        if not chunk:
            return _result(SummaryOutcome.NOTHING_TO_DO, SummaryReason.NO_NEW_MESSAGES)
        previous_data = None if previous is None else previous.data
        try:
            prompt = build_summary_prompt(previous_data, chunk, max_chars=self._prompt_max_chars)
        except SummaryPromptTooLarge as exc:
            return await self._failed(contact_id, SummaryReason.PROMPT_TOO_LARGE, exc)
        try:
            extracted = await self._client.complete_json(prompt.messages, ContactSummary)
        except LLMError as exc:
            return await self._failed(contact_id, SummaryReason.LLM_ERROR, exc)
        except ValidationError as exc:
            return await self._failed(contact_id, SummaryReason.INVALID_OUTPUT, exc)
        sanitized = sanitize_summary(extracted)
        data = sanitized.summary
        reason = SummaryReason.UPDATED
        if data.is_empty():
            if previous_data is None:
                await self._summaries.mark_summary_failed(contact_id, self._clock())
                return _result(
                    SummaryOutcome.NOTHING_TO_DO,
                    SummaryReason.EMPTY_RESULT,
                    messages=len(chunk),
                    messages_in_prompt=prompt.messages_kept,
                    removed=sanitized.removed,
                )
            data = previous_data
            reason = SummaryReason.KEPT_PREVIOUS
        new_cursor = _cursor_of(chunk[-1])
        stored = StoredSummary(
            version=SUMMARY_VERSION, updated_at=self._clock(), cursor=new_cursor, data=data
        )
        try:
            written = await self._summaries.store_summary(contact_id, stored, cursor)
        except ContactNotFound:
            return _result(SummaryOutcome.CONTACT_MISSING, SummaryReason.CONTACT_MISSING)
        if not written:
            return _result(
                SummaryOutcome.NOTHING_TO_DO,
                SummaryReason.CONCURRENT_UPDATE,
                messages=len(chunk),
                messages_in_prompt=prompt.messages_kept,
            )
        return _result(
            SummaryOutcome.UPDATED,
            reason,
            messages=len(chunk),
            messages_in_prompt=prompt.messages_kept,
            remaining=await self._summaries.count_after(contact_id, new_cursor),
            removed=sanitized.removed,
        )

    async def _failed(
        self, contact_id: int, reason: SummaryReason, error: Exception
    ) -> SummaryUpdateResult:
        try:
            await self._summaries.mark_summary_failed(contact_id, self._clock())
        except Exception as exc:
            logger.warning(
                "summary failure was not recorded",
                extra={"contact_id": contact_id, "error": type(exc).__name__},
            )
        return _result(SummaryOutcome.FAILED, reason, error=type(error).__name__)


def _result(
    outcome: SummaryOutcome,
    reason: SummaryReason,
    *,
    messages: int = 0,
    messages_in_prompt: int = 0,
    remaining: int | None = None,
    removed: int = 0,
    error: str | None = None,
) -> SummaryUpdateResult:
    return SummaryUpdateResult(
        outcome=outcome,
        reason=reason,
        messages=messages,
        messages_in_prompt=messages_in_prompt,
        remaining=remaining,
        removed=removed,
        error=error,
    )


def _log(contact_id: int, result: SummaryUpdateResult) -> None:
    level = logging.WARNING if result.outcome is SummaryOutcome.FAILED else logging.INFO
    logger.log(
        level,
        "summary update finished",
        extra={
            "contact_id": contact_id,
            "outcome": result.outcome.value,
            "reason": result.reason.value,
            "messages": result.messages,
            "messages_in_prompt": result.messages_in_prompt,
            "remaining": result.remaining,
            "removed": result.removed,
            "error": result.error,
        },
    )
