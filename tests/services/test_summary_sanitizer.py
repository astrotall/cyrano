from __future__ import annotations

import pytest

from cyrano.domain.summary import ContactSummary
from cyrano.services.summary_sanitizer import contains_contact_detail, sanitize_summary

CONTACT_DETAILS = [
    "мой номер +7 (999) 123-45-67",
    "звони 8 999 1234567",
    "123 45 67",
    "1234567",
    "пиши на invented.person@example.org",
    "INVENTED@EXAMPLE.COM",
    "https://example.org/page",
    "http://example.org",
    "www.example.org",
    "t.me/invented_handle",
    "vk.com/invented",
    "example.org/profile",
    "мой ник @invented_zq",
    "мой ник @Invented",
    "@inv​ented",
]
ORDINARY = [
    "ей 25 лет",
    "родилась в 1998 году",
    "2 кошки и 3 собаки",
    "живёт на 5 этаже",
    "встреча в 19:30",
    "дата 12.05.2026",
    "рост 170, вес 60",
    "е-мейл не дала",
    "@abc",
    "кофе в 9 и в 12",
]


@pytest.mark.parametrize("text", CONTACT_DETAILS)
def test_contact_details_are_recognised(text: str) -> None:
    assert contains_contact_detail(text)


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_numbers_and_words_survive(text: str) -> None:
    assert not contains_contact_detail(text)


def test_contact_details_are_removed_from_lists_and_cleared_in_texts() -> None:
    summary = ContactSummary(
        name="Оля @olya_invented",
        age=25,
        city="Казань",
        occupation="дизайнер, сайт www.example.org",
        interests=["велосипед", "пиши в t.me/invented", "кино"],
        mood_and_tone="спокойное",
        agreements_and_plans=["созвониться: +7 999 123 45 67"],
        open_questions=["любит ли собак"],
        told_about_owner=["владелец работает программистом", "мой мейл me@example.org"],
        notes=["родилась в 1998", "http://example.org/x"],
    )

    sanitized = sanitize_summary(summary)

    result = sanitized.summary
    assert result.name is None
    assert result.occupation is None
    assert result.city == "Казань"
    assert result.age == 25
    assert result.interests == ["велосипед", "кино"]
    assert result.agreements_and_plans == []
    assert result.open_questions == ["любит ли собак"]
    assert result.told_about_owner == ["владелец работает программистом"]
    assert result.notes == ["родилась в 1998"]
    assert result.mood_and_tone == "спокойное"
    assert sanitized.removed == 6


def test_a_clean_summary_is_unchanged() -> None:
    summary = ContactSummary(name="Оля", age=30, interests=["кино"], notes=["2 кошки"])

    sanitized = sanitize_summary(summary)

    assert sanitized.summary == summary
    assert sanitized.removed == 0


def test_a_summary_of_contact_details_only_becomes_empty() -> None:
    summary = ContactSummary(name="@invented_handle", notes=["+7 999 123 45 67"])

    sanitized = sanitize_summary(summary)

    assert sanitized.summary.is_empty()
    assert sanitized.removed == 2


def test_the_sanitiser_reuses_the_stop_trigger_patterns() -> None:
    from cyrano.services import stop_triggers, summary_sanitizer

    for name in ("EMAIL", "HANDLE", "CONTACT_LINK", "DIGIT_RUN"):
        assert getattr(summary_sanitizer, name) is getattr(stop_triggers, name)
