from __future__ import annotations

from datetime import UTC, datetime

from cyrano.domain.models import IncomingMessage
from cyrano.services.sanitize import NUL, sanitize_incoming, strip_nul, strip_nul_optional


def _incoming(**overrides: object) -> IncomingMessage:
    fields: dict[str, object] = {
        "tg_id": 10,
        "username": "fake_user",
        "display_name": "Fake Name",
        "tg_message_id": 5,
        "text": "fake text",
        "sent_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    return IncomingMessage.model_validate({**fields, **overrides})


def test_strip_nul_removes_every_nul_byte() -> None:
    assert strip_nul(f"a{NUL}b{NUL}{NUL}c") == "abc"
    assert strip_nul(NUL) == ""


def test_strip_nul_leaves_other_text_untouched() -> None:
    text = "fake кириллица 🙂\n\t  ​ end"

    assert strip_nul(text) == text
    assert strip_nul("") == ""


def test_strip_nul_optional_keeps_none() -> None:
    assert strip_nul_optional(None) is None
    assert strip_nul_optional(f"x{NUL}") == "x"


def test_sanitize_incoming_cleans_text_username_and_display_name() -> None:
    cleaned = sanitize_incoming(
        _incoming(text=f"te{NUL}xt", username=f"{NUL}user", display_name=f"na{NUL}me")
    )

    assert cleaned.text == "text"
    assert cleaned.username == "user"
    assert cleaned.display_name == "name"


def test_sanitize_incoming_keeps_the_rest_and_missing_values() -> None:
    original = _incoming(username=None, display_name=None, text="")

    assert sanitize_incoming(original) == original
