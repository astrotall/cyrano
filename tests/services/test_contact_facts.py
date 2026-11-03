from __future__ import annotations

from cyrano.domain.reply import (
    FACT_ABOUT_LIMIT,
    FACT_BIO_LIMIT,
    FACT_CITY_LIMIT,
    FACT_NAME_LIMIT,
    ContactFacts,
)
from cyrano.services.contact_facts import build_contact_facts
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.reply_data import (
    FAKE_MATCH_TEXT,
    FAKE_PHOTO_PATH,
    FAKE_RAW_CARD,
    FAKE_USERNAME,
    build_prompt,
    make_contact,
)


def _profile(**leomatch: object) -> dict[str, object]:
    return {
        "username": FAKE_USERNAME,
        "display_name": "Profile Display",
        "bio": "Invented bio",
        "photo": FAKE_PHOTO_PATH,
        "leomatch": {
            "raw_text": FAKE_RAW_CARD,
            "match_text": FAKE_MATCH_TEXT,
            "name": "Cardname",
            "name_source": "card",
            "age": 27,
            "city": "Inventedtown",
            "about": "Invented about",
            "photos": [FAKE_PHOTO_PATH],
            **leomatch,
        },
    }


def test_a_full_profile_gives_the_five_allowed_facts() -> None:
    facts = build_contact_facts(make_contact(_profile()))

    assert facts == ContactFacts(
        name="Cardname", age=27, city="Inventedtown", about="Invented about", bio="Invented bio"
    )


def test_no_forbidden_value_is_part_of_the_facts() -> None:
    dumped = build_contact_facts(make_contact(_profile())).model_dump_json()

    for forbidden in (FAKE_USERNAME, FAKE_PHOTO_PATH, FAKE_RAW_CARD, FAKE_MATCH_TEXT):
        assert forbidden not in dumped


def test_the_card_name_wins_over_the_display_names() -> None:
    assert build_contact_facts(make_contact(_profile())).name == "Cardname"


def test_the_match_message_name_is_used_when_the_card_has_none() -> None:
    profile = _profile(name="Matchname", name_source="match")

    assert build_contact_facts(make_contact(profile)).name == "Matchname"


def test_the_contact_display_name_follows_then_the_profile_one() -> None:
    no_leomatch_name = _profile(name=None)
    assert build_contact_facts(make_contact(no_leomatch_name)).name == "Contactfirst Contactlast"
    assert (
        build_contact_facts(make_contact(no_leomatch_name, display_name=None)).name
        == "Profile Display"
    )


def test_empty_fields_stay_empty() -> None:
    assert build_contact_facts(make_contact({}, display_name=None)) == ContactFacts()


def test_blank_fields_count_as_absent() -> None:
    profile = _profile(city="   ", about="")
    profile["bio"] = "\n "

    facts = build_contact_facts(make_contact(profile))

    assert (facts.city, facts.about, facts.bio) == (None, None, None)


def test_each_fact_is_truncated_to_its_named_limit() -> None:
    profile = _profile(name="n" * 100, city="c" * 100, about="a" * 400)
    profile["bio"] = "b" * 500

    facts = build_contact_facts(make_contact(profile))

    assert facts.name is not None
    assert len(facts.name) == FACT_NAME_LIMIT
    assert facts.city is not None
    assert len(facts.city) == FACT_CITY_LIMIT
    assert facts.about is not None
    assert len(facts.about) == FACT_ABOUT_LIMIT
    assert facts.bio is not None
    assert len(facts.bio) == FACT_BIO_LIMIT


def test_the_limits_are_the_documented_ones() -> None:
    limits = (FACT_NAME_LIMIT, FACT_CITY_LIMIT, FACT_ABOUT_LIMIT, FACT_BIO_LIMIT)

    assert limits == (60, 60, 300, 300)


def test_a_profile_that_does_not_validate_gives_only_the_display_name() -> None:
    facts = build_contact_facts(make_contact({"age": "not a profile", "bio": 5}))

    assert facts == ContactFacts(name="Contactfirst Contactlast")


def test_the_rendered_prompt_carries_none_of_the_forbidden_values() -> None:
    contact = make_contact(_profile())
    prompt = build_prompt(facts=build_contact_facts(contact))

    text = "\n".join(message.content for message in prompt.messages)

    for forbidden in (FAKE_USERNAME, FAKE_PHOTO_PATH, FAKE_RAW_CARD, FAKE_MATCH_TEXT):
        assert forbidden not in text
    assert_absent(FAKE_TELEGRAM_ID, text)
    assert "- Name: Cardname" in text
