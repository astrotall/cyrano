"""Assembly of the reply prompt and trimming it to the character budget."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from cyrano.domain.enums import MessageDirection, Role
from cyrano.domain.errors import NoMessagesToReplyTo, PromptTooLarge
from cyrano.domain.llm import LLMMessage
from cyrano.domain.models import Message
from cyrano.domain.reply import MIN_HISTORY_MESSAGES, BuiltPrompt, ContactFacts
from cyrano.domain.style import StyleExample
from cyrano.prompts.render import render_system_prompt, render_user_prompt


class PromptBuilder:
    def __init__(
        self,
        *,
        budget_chars: int,
        timezone: ZoneInfo,
        clock: Callable[[], datetime],
    ) -> None:
        self._budget_chars = budget_chars
        self._timezone = timezone
        self._clock = clock

    def build(
        self,
        *,
        contact_id: int,
        persona: str,
        mode_fragment: str,
        facts: ContactFacts,
        summary: str | None,
        messages: Sequence[Message],
        examples: Sequence[StyleExample],
    ) -> BuiltPrompt:
        if not any(message.direction is MessageDirection.INCOMING for message in messages):
            raise NoMessagesToReplyTo(contact_id)
        now = self._clock().astimezone(self._timezone)
        floor = min(MIN_HISTORY_MESSAGES, len(messages))
        examples_kept = len(examples)
        messages_kept = len(messages)
        while True:
            prompt = self._render(
                now=now,
                persona=persona,
                mode_fragment=mode_fragment,
                facts=facts,
                summary=summary,
                messages=messages[len(messages) - messages_kept :],
                examples=examples[:examples_kept],
            )
            characters = sum(len(message.content) for message in prompt)
            if characters <= self._budget_chars:
                return BuiltPrompt(
                    messages=prompt,
                    examples_offered=len(examples),
                    examples_kept=examples_kept,
                    messages_offered=len(messages),
                    messages_kept=messages_kept,
                    characters=characters,
                )
            if examples_kept > 0:
                examples_kept -= 1
            elif messages_kept > floor:
                messages_kept -= 1
            else:
                raise PromptTooLarge(characters, self._budget_chars)

    def _render(
        self,
        *,
        now: datetime,
        persona: str,
        mode_fragment: str,
        facts: ContactFacts,
        summary: str | None,
        messages: Sequence[Message],
        examples: Sequence[StyleExample],
    ) -> tuple[LLMMessage, ...]:
        system = render_system_prompt(
            persona=persona,
            mode_fragment=mode_fragment,
            facts=facts,
            summary=summary,
            examples=examples,
        )
        user = render_user_prompt(now=now, timezone_name=self._timezone.key, messages=messages)
        return (
            LLMMessage(role=Role.SYSTEM, content=system),
            LLMMessage(role=Role.USER, content=user),
        )
