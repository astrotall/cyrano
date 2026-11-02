"""Removal of characters that PostgreSQL text and JSONB columns reject."""

from __future__ import annotations

from cyrano.domain.models import IncomingMessage

NUL = "\x00"


def strip_nul(value: str) -> str:
    return value.replace(NUL, "")


def strip_nul_optional(value: str | None) -> str | None:
    return None if value is None else strip_nul(value)


def clean_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return strip_nul(value).strip()[:limit] or None


def sanitize_incoming(incoming: IncomingMessage) -> IncomingMessage:
    return incoming.model_copy(
        update={
            "text": strip_nul(incoming.text),
            "username": strip_nul_optional(incoming.username),
            "display_name": strip_nul_optional(incoming.display_name),
        }
    )
