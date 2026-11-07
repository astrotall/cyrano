"""The one rule that turns the time of a Telegram message into the time that is stored."""

from __future__ import annotations

from datetime import datetime

from cyrano.domain.models import MAX_FUTURE_SKEW


def message_time(sent_at: datetime | None, now: datetime) -> datetime:
    if sent_at is None or sent_at - now > MAX_FUTURE_SKEW:
        return now
    return sent_at
