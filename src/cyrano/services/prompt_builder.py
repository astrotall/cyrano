from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cyrano.domain.enums import MessageDirection, ReplyPurpose, Role
from cyrano.domain.errors import NoMessagesToReplyTo, PromptTooLarge
from cyrano.domain.llm import LLMMessage
from cyrano.domain.models import Message
from cyrano.domain.reply import MIN_HISTORY_MESSAGES, BuiltPrompt, ContactFacts, MessageRef
from cyrano.domain.schedule import UnavailableSoon
from cyrano.domain.style import StyleExample
from cyrano.prompts.render import (
    render_proactive_request,
    render_system_prompt,
    render_user_prompt,
    wrapup_reason,
)
from cyrano.prompts.texts import NEXT_MESSAGE_REQUEST

ONE_HOUR = timedelta(hours=1)


def wrapup_reason_text(soon: UnavailableSoon) -> str:
    return wrapup_reason(soon)


def silence_hours(now: datetime, messages: Sequence[Message]) -> int:
    if not messages:
        return 0
    return max(0, (now - messages[-1].created_at) // ONE_HOUR)


def message_refs(messages: Sequence[Message]) -> dict[int, MessageRef]:
    return {
        number: MessageRef(tg_message_id=message.tg_message_id, direction=message.direction)
        for number, message in enumerate(messages, start=1)
    }


class PromptBuilder:
    def __init__(
        self,
        *,
        budget_chars: int,
        timezone: ZoneInfo,
        clock: Callable[[], datetime],
        quote_enabled: bool = False,
    ) -> None:
        self._budget_chars = budget_chars
        self._timezone = timezone
        self._clock = clock
        self._quote_enabled = quote_enabled

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
        purpose: ReplyPurpose = ReplyPurpose.REPLY,
        wrapup: UnavailableSoon | None = None,
    ) -> BuiltPrompt:
        if not any(message.direction is MessageDirection.INCOMING for message in messages):
            raise NoMessagesToReplyTo(contact_id)
        moment = self._clock()
        now = moment.astimezone(self._timezone)
        proactive_hours = (
            silence_hours(moment, messages) if purpose is ReplyPurpose.PROACTIVE else None
        )
        numbered = self._quote_enabled and purpose is ReplyPurpose.REPLY
        floor = min(MIN_HISTORY_MESSAGES, len(messages))
        examples_kept = len(examples)
        messages_kept = len(messages)
        while True:
            shown = messages[len(messages) - messages_kept :]
            prompt = self._render(
                now=now,
                persona=persona,
                mode_fragment=mode_fragment,
                facts=facts,
                summary=summary,
                messages=shown,
                examples=examples[:examples_kept],
                proactive_hours=proactive_hours,
                wrapup=wrapup,
                numbered=numbered,
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
                    purpose=purpose,
                    wrap_up=wrapup is not None,
                    message_refs=message_refs(shown) if numbered else {},
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
        proactive_hours: int | None,
        wrapup: UnavailableSoon | None,
        numbered: bool,
    ) -> tuple[LLMMessage, ...]:
        system = render_system_prompt(
            persona=persona,
            mode_fragment=mode_fragment,
            facts=facts,
            summary=summary,
            examples=examples,
            proactive_hours=proactive_hours,
            wrapup_phase=None if wrapup is None else wrapup.phase,
            quote_markers=numbered,
        )
        user = render_user_prompt(
            now=now,
            timezone_name=self._timezone.key,
            messages=messages,
            request=NEXT_MESSAGE_REQUEST if proactive_hours is None else render_proactive_request(),
            wrapup=wrapup,
            numbered=numbered,
        )
        return (
            LLMMessage(role=Role.SYSTEM, content=system),
            LLMMessage(role=Role.USER, content=user),
        )
