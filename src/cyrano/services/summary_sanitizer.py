from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from cyrano.domain.summary import (
    SUMMARY_LINK_PREFIXES,
    SUMMARY_LIST_CAPS,
    SUMMARY_PHONE_MIN_DIGITS,
    SUMMARY_TEXT_FIELDS,
    ContactSummary,
)
from cyrano.services.stop_triggers import CONTACT_LINK, DIGIT_RUN, EMAIL, HANDLE, normalize

WEB_LINK: Final = re.compile(
    "(?:" + "|".join(re.escape(prefix) for prefix in SUMMARY_LINK_PREFIXES) + ")"
)
DOMAIN_WITH_PATH: Final = re.compile(r"(?<![\w.@-])(?:[a-z0-9-]+\.)+[a-z]{2,}/")


@dataclass(frozen=True, slots=True)
class SanitizedSummary:
    summary: ContactSummary
    removed: int


def _digits(run: str) -> int:
    return sum(1 for character in run if character.isdecimal())


def contains_contact_detail(text: str) -> bool:
    base = normalize(text).base
    if any(
        pattern.search(base)
        for pattern in (EMAIL, HANDLE, CONTACT_LINK, WEB_LINK, DOMAIN_WITH_PATH)
    ):
        return True
    return any(
        _digits(match.group()) >= SUMMARY_PHONE_MIN_DIGITS for match in DIGIT_RUN.finditer(base)
    )


def sanitize_summary(summary: ContactSummary) -> SanitizedSummary:
    removed = 0
    data: dict[str, object] = {"age": summary.age}
    for name in SUMMARY_TEXT_FIELDS:
        value: str | None = getattr(summary, name)
        if value is not None and contains_contact_detail(value):
            removed += 1
            value = None
        data[name] = value
    for name in SUMMARY_LIST_CAPS:
        items: list[str] = getattr(summary, name)
        kept = [item for item in items if not contains_contact_detail(item)]
        removed += len(items) - len(kept)
        data[name] = kept
    return SanitizedSummary(summary=ContactSummary.model_validate(data), removed=removed)
