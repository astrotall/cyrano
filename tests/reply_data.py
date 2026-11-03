from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from cyrano.domain.enums import ContactStatus, MessageDirection
from cyrano.domain.models import Contact, Message
from cyrano.domain.reply import BuiltPrompt, ContactFacts
from cyrano.domain.style import StyleExample
from cyrano.services.prompt_builder import PromptBuilder
from tests.privacy import FAKE_TELEGRAM_ID

MOSCOW = ZoneInfo("Europe/Moscow")
FIXED_NOW = datetime(2026, 11, 3, 11, 5, tzinfo=UTC)
BIG_BUDGET = 1_000_000
PERSONA_TEXT = (
    "Persona line one: invented, friendly, writes short.\nPersona line two: likes cycling."
)
MODE_FRAGMENT = "Mode fragment: keep it light and playful."
FAKE_USERNAME = "invented_handle_zq"
FAKE_PHOTO_PATH = "profiles/invented-photo-zq.jpg"
FAKE_RAW_CARD = "RAW-CARD-TEXT-zq invented raw card"
FAKE_MATCH_TEXT = "RAW-MATCH-TEXT-zq invented match"
INCOMING = MessageDirection.INCOMING
OUTGOING = MessageDirection.OUTGOING


def fixed_clock() -> datetime:
    return FIXED_NOW


def make_message(index: int, direction: MessageDirection, text: str) -> Message:
    return Message(
        id=index,
        contact_id=7,
        direction=direction,
        text=text,
        tg_message_id=1000 + index,
        created_at=FIXED_NOW - timedelta(minutes=100 - index),
    )


def make_history(*items: tuple[MessageDirection, str]) -> list[Message]:
    return [make_message(index, direction, text) for index, (direction, text) in enumerate(items)]


def make_example(*turns: tuple[str, str], tags: Sequence[str] = ()) -> StyleExample:
    return StyleExample.model_validate(
        {
            "id": 1,
            "tags": list(tags),
            "turns": [{"role": role, "text": text} for role, text in turns],
            "created_at": FIXED_NOW,
        }
    )


def make_examples(count: int) -> list[StyleExample]:
    return [
        make_example(("them", f"example question {number}"), ("me", f"example answer {number}"))
        for number in range(1, count + 1)
    ]


def make_contact(profile: dict[str, object] | None = None, **overrides: object) -> Contact:
    fields: dict[str, object] = {
        "id": 7,
        "tg_id": FAKE_TELEGRAM_ID,
        "username": FAKE_USERNAME,
        "display_name": "Contactfirst Contactlast",
        "status": ContactStatus.ACTIVE,
        "mode_id": 1,
        "profile": profile if profile is not None else {},
        "summary": {},
        "last_message_at": FIXED_NOW,
        "created_at": FIXED_NOW,
        "updated_at": FIXED_NOW,
    }
    return Contact.model_validate({**fields, **overrides})


def make_builder(budget: int = BIG_BUDGET) -> PromptBuilder:
    return PromptBuilder(budget_chars=budget, timezone=MOSCOW, clock=fixed_clock)


def default_history() -> list[Message]:
    return make_history(
        (INCOMING, "hello there"),
        (OUTGOING, "hi, how are you"),
        (INCOMING, "fine, and you?"),
    )


def build_prompt(
    *,
    budget: int = BIG_BUDGET,
    persona: str = PERSONA_TEXT,
    mode_fragment: str = MODE_FRAGMENT,
    facts: ContactFacts | None = None,
    summary: str | None = None,
    messages: Sequence[Message] | None = None,
    examples: Sequence[StyleExample] | None = None,
) -> BuiltPrompt:
    return make_builder(budget).build(
        contact_id=7,
        persona=persona,
        mode_fragment=mode_fragment,
        facts=facts if facts is not None else ContactFacts(),
        summary=summary,
        messages=messages if messages is not None else default_history(),
        examples=examples if examples is not None else [],
    )


def system_of(prompt: BuiltPrompt) -> str:
    return prompt.messages[0].content


def user_of(prompt: BuiltPrompt) -> str:
    return prompt.messages[1].content
