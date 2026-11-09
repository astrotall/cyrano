from __future__ import annotations

import random
import re
import unicodedata

import pytest

from cyrano.domain.enums import TriggerClass
from cyrano.domain.stop_triggers import DETECTOR_MAX_TEXT_CHARS, StopDecision
from cyrano.services.stop_triggers import (
    CLASS_PATTERNS,
    PatternSyntaxError,
    classes_of,
    compile_phrase,
    detect,
    normalize,
)
from cyrano.stop_trigger_patterns import (
    ALTERNATIVE_SEPARATOR,
    CONTACT_CHANNELS,
    GAP_MARK,
    OPTIONAL_PREFIX,
    STEM_SUFFIX,
    WORD_PATTERNS,
)
from tests.services.stop_trigger_phrases import ACCEPTED_FALSE_POSITIVES, NEGATIVES, POSITIVES

MIN_PHRASES_PER_CLASS = 15
NON_TEXT_CLASSES = frozenset({TriggerClass.MEDIA})
FUZZ_SEED = 20261106
FUZZ_ROUNDS = 300
FUZZ_ALPHABET = (
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВЁЖЯabcxyzXYZ0123456789 \t\n.,!?@+-_()*/:;\"'«»​‍﻿­́̈⃣️😀🔥❤️₽$€ﷺ①ⅷ"
)


def _cases(table: dict[TriggerClass, tuple[str, ...]]) -> list[tuple[TriggerClass, str]]:
    return [(trigger, phrase) for trigger, phrases in table.items() for phrase in phrases]


def test_every_class_has_enough_positive_and_negative_phrases() -> None:
    for trigger in set(TriggerClass) - NON_TEXT_CLASSES:
        assert len(POSITIVES[trigger]) >= MIN_PHRASES_PER_CLASS
        assert len(NEGATIVES[trigger]) >= MIN_PHRASES_PER_CLASS


def test_the_media_class_is_never_detected_from_a_text() -> None:
    phrases = [
        phrase for table in (POSITIVES, NEGATIVES) for items in table.values() for phrase in items
    ]

    assert phrases
    for phrase in phrases:
        assert TriggerClass.MEDIA not in detect(phrase).classes


@pytest.mark.parametrize(("trigger", "phrase"), _cases(POSITIVES))
def test_a_positive_phrase_triggers_its_class(trigger: TriggerClass, phrase: str) -> None:
    decision = detect(phrase)

    assert decision.triggered
    assert trigger in decision.classes


@pytest.mark.parametrize(("trigger", "phrase"), _cases(NEGATIVES))
def test_a_close_but_harmless_phrase_does_not_trigger_its_class(
    trigger: TriggerClass, phrase: str
) -> None:
    assert trigger not in detect(phrase).classes


@pytest.mark.parametrize(("phrase", "trigger"), ACCEPTED_FALSE_POSITIVES)
def test_the_documented_false_positives_are_accepted(phrase: str, trigger: TriggerClass) -> None:
    assert trigger in detect(phrase).classes


@pytest.mark.parametrize(
    "phrase",
    ["", "   ", "привет", "Привет! Как дела?", "ок", "😀😀😀", "...", "хорошего дня", "спасибо"],
)
def test_ordinary_small_talk_triggers_nothing(phrase: str) -> None:
    decision = detect(phrase)

    assert decision == StopDecision()
    assert not decision.triggered


@pytest.mark.parametrize(
    "phrase",
    [
        "ТЫ БОТ?",
        "Ты Бот",
        "ты бооооот?",
        "ты бот!!!???",
        "ты-бот",
        "ты, бот?",
        "ты 🤖 бот",
        "ты​ бот",
        "т‍ы б﻿от",
        "ты б­от",
        "ты б о т",
        "ты\tбот",
        "ты\n\nбот",
        "ты！бот？",
        "ты бо́т",
    ],
)
def test_normalisation_handles_case_repeats_punctuation_emoji_and_invisible_characters(
    phrase: str,
) -> None:
    assert TriggerClass.BOT_QUESTION in detect(phrase).classes


def test_yo_is_read_as_ye() -> None:
    assert TriggerClass.MEETING in detect("пойдём в кино").classes
    assert TriggerClass.MEETING in detect("ПОЙДЁМ").classes
    assert TriggerClass.MEETING in detect("пойдём").classes


def test_spaced_out_letters_are_joined() -> None:
    assert TriggerClass.BOT_QUESTION in detect("н е й р о с е т ь").classes
    assert TriggerClass.PHOTO in detect("скинь ф о т о").classes
    assert TriggerClass.MONEY in detect("д е н ь г и нужны").classes


def test_two_single_letters_are_not_joined() -> None:
    assert normalize("ты и я").words == (" ты и я ",)


def test_runs_of_three_or_more_letters_keep_two_and_a_second_view_keeps_one() -> None:
    normalized = normalize("приииивет")

    assert normalized.words == (" приивет ",)
    assert normalized.single_letter_words == (" привет ",)


def test_a_legitimate_double_letter_still_matches_through_the_full_view() -> None:
    assert TriggerClass.EXES in detect("мы расстались").classes
    assert TriggerClass.EXES in detect("мы рассссстались").classes


def test_the_single_view_never_turns_ii_into_a_conjunction_match() -> None:
    assert TriggerClass.BOT_QUESTION not in detect("ты и я и он").classes
    assert TriggerClass.BOT_QUESTION in detect("ты ииии?").classes


@pytest.mark.parametrize(
    "phrase",
    [
        "+7 900 123 45 67",
        "+7(900)123-45-67",
        "8-900-123-45-67",
        "8 (900) 123 45 67",
        "89001234567",
        "+79001234567",
        "9001234567",
        "+44 20 7946 0958",
        "мой 8 900 123 4567",
        "8–900–123–45–67",
        "１２３４５６７８９０",
        "123 45 67",
        "123-45-67",
    ],
)
def test_phone_numbers_in_every_format_are_contact_exchange(phrase: str) -> None:
    assert TriggerClass.CONTACT_EXCHANGE in detect(phrase).classes


@pytest.mark.parametrize(
    "phrase",
    ["мне 25", "в 19:30", "12.05.2024", "с 2020 по 2024", "1234567", "дом 15 кв 7", "1.2.3.4.5.6"],
)
def test_short_numbers_dates_and_times_are_not_phones(phrase: str) -> None:
    assert TriggerClass.CONTACT_EXCHANGE not in detect(phrase).classes


@pytest.mark.parametrize(
    "phrase",
    [
        "4276 1234 5678 9012",
        "4276-1234-5678-9012",
        "4276123456789012",
        "5469 3800 1234 5678 901",
        "1234567890123",
    ],
)
def test_card_like_digit_runs_are_money(phrase: str) -> None:
    assert TriggerClass.MONEY in detect(phrase).classes


def test_a_twenty_digit_run_is_a_contact_and_not_a_card() -> None:
    classes = detect("12345678901234567890").classes

    assert TriggerClass.CONTACT_EXCHANGE in classes
    assert TriggerClass.MONEY not in classes


def test_a_twelve_digit_run_is_a_contact_and_not_a_card() -> None:
    classes = detect("123456789012").classes

    assert TriggerClass.CONTACT_EXCHANGE in classes
    assert TriggerClass.MONEY not in classes


@pytest.mark.parametrize(
    "phrase",
    [
        "fake.person@example.org",
        "пиши FAKE_PERSON+tag@example.co.uk",
        "почта: a.b-c@sub.example.org",
        "fake собака gmail ru",
    ],
)
def test_e_mail_addresses_are_contact_exchange(phrase: str) -> None:
    assert TriggerClass.CONTACT_EXCHANGE in detect(phrase).classes


@pytest.mark.parametrize("phrase", ["@fake_handle", "мой @Fake_Zq_42", "пиши @abcd"])
def test_handles_of_four_or_more_characters_are_contact_exchange(phrase: str) -> None:
    assert TriggerClass.CONTACT_EXCHANGE in detect(phrase).classes


@pytest.mark.parametrize("phrase", ["@abc", "@", "собака@", "@ привет"])
def test_short_or_empty_handles_are_not_contact_exchange(phrase: str) -> None:
    assert TriggerClass.CONTACT_EXCHANGE not in detect(phrase).classes


@pytest.mark.parametrize(
    "phrase",
    ["t.me/fake_zq", "https://vk.com/fake_zq", "instagram.com/fake_zq", "wa.me/79001234567"],
)
def test_links_to_profiles_are_contact_exchange(phrase: str) -> None:
    assert TriggerClass.CONTACT_EXCHANGE in detect(phrase).classes


@pytest.mark.parametrize(
    "name",
    ["вотсап", "ватсап", "вацап", "whatsapp", "viber", "вайбер", "телега", "тг", "инста", "вк"],
)
def test_a_messenger_name_alone_is_not_contact_exchange(name: str) -> None:
    assert TriggerClass.CONTACT_EXCHANGE not in detect(f"я люблю {name}").classes


@pytest.mark.parametrize(
    "name",
    ["вотсап", "ватсап", "вацап", "whatsapp", "viber", "вайбер", "телегу", "тг", "инсту", "вк"],
)
def test_a_messenger_name_with_a_request_verb_is_contact_exchange(name: str) -> None:
    assert TriggerClass.CONTACT_EXCHANGE in detect(f"напиши мне в {name}").classes
    assert TriggerClass.CONTACT_EXCHANGE in detect(f"давай в {name}").classes


def test_a_messenger_name_with_a_handle_or_a_number_is_contact_exchange() -> None:
    assert TriggerClass.CONTACT_EXCHANGE in detect("инста @fake_zq").classes
    assert TriggerClass.CONTACT_EXCHANGE in detect("вотсап 89001234567").classes


def test_a_request_verb_far_from_the_messenger_name_does_not_pair() -> None:
    phrase = "напиши что ты любишь делать по вечерам кроме телеги"

    assert TriggerClass.CONTACT_EXCHANGE not in detect(phrase).classes


@pytest.mark.parametrize("symbol", ["₽", "$", "€", "£", "¥", "＄"])
def test_currency_symbols_are_money(symbol: str) -> None:
    assert TriggerClass.MONEY in detect(f"это 100{symbol}").classes


def test_several_texts_are_combined_into_one_decision() -> None:
    decision = detect(["привет", "давай встретимся", "скинь фото"])

    assert decision.classes == frozenset({TriggerClass.MEETING, TriggerClass.PHOTO})


def test_one_text_and_a_list_with_that_text_give_the_same_decision() -> None:
    assert detect("ты бот?") == detect(["ты бот?"])


def test_an_empty_list_triggers_nothing() -> None:
    assert detect([]) == StopDecision()


def test_one_text_can_trigger_several_classes() -> None:
    classes = detect("ты бот? скинь фото и номер карты").classes

    assert {TriggerClass.BOT_QUESTION, TriggerClass.PHOTO, TriggerClass.MONEY} <= classes


def test_the_decision_lists_classes_in_the_enum_order() -> None:
    decision = detect("скинь фото, ты бот?")

    assert decision.ordered() == (TriggerClass.PHOTO, TriggerClass.BOT_QUESTION)


def test_a_huge_text_is_truncated_before_matching_and_never_raises() -> None:
    trigger_after_the_cap = "а" * DETECTOR_MAX_TEXT_CHARS + " ты бот"
    trigger_inside_the_cap = "ты бот " + "а" * (DETECTOR_MAX_TEXT_CHARS * 50)

    assert detect(trigger_after_the_cap) == StopDecision()
    assert TriggerClass.BOT_QUESTION in detect(trigger_inside_the_cap).classes
    assert detect("9" * 1_000_000).triggered


@pytest.mark.parametrize(
    "text",
    [
        "\ud800",
        "\udfff ты бот \ud800",
        "\x00\x01\x02",
        "‮тоб ыт",
        "ﷺ" * 5000,
        "́" * 10000,
        "​" * 10000,
        " " * 100000,
        "@" * 5000,
        "+" + "-" * 5000 + "1",
        "1 " * 5000,
        "ты " * 3000,
        "давай " * 3000 + "встретимся",
        "😀" * 10000,
        "\U0010ffff\U000e0001",
    ],
)
def test_odd_input_never_raises(text: str) -> None:
    decision = detect(text)

    assert isinstance(decision, StopDecision)


def test_random_input_never_raises() -> None:
    generator = random.Random(FUZZ_SEED)
    for _ in range(FUZZ_ROUNDS):
        length = generator.randint(0, 400)
        text = "".join(generator.choice(FUZZ_ALPHABET) for _ in range(length))
        assert isinstance(detect(text), StopDecision)


def test_every_class_has_compiled_word_patterns() -> None:
    assert set(CLASS_PATTERNS) == set(TriggerClass) - NON_TEXT_CLASSES
    for patterns in CLASS_PATTERNS.values():
        assert patterns.full is not None
        assert patterns.single is not None


def _pattern_words(phrase: str) -> list[str]:
    words: list[str] = []
    for token in phrase.split():
        if re.fullmatch(rf"{re.escape(GAP_MARK)}\d?", token):
            continue
        alternatives = token.removeprefix(OPTIONAL_PREFIX).split(ALTERNATIVE_SEPARATOR)
        words.extend(word.removesuffix(STEM_SUFFIX) for word in alternatives)
    return words


def test_the_pattern_data_is_written_in_the_normalised_form() -> None:
    for phrases in WORD_PATTERNS.values():
        for phrase in phrases:
            for word in _pattern_words(phrase):
                assert word
                assert word == unicodedata.normalize("NFKC", word).casefold()
                assert "ё" not in word
                assert re.fullmatch(r"[^\W_]+", word), word


def test_the_pattern_data_has_no_duplicate_phrase_in_a_class() -> None:
    for phrases in WORD_PATTERNS.values():
        assert len(phrases) == len(set(phrases))


def test_every_pattern_compiles_on_its_own() -> None:
    for phrases in WORD_PATTERNS.values():
        for phrase in phrases:
            re.compile(compile_phrase(phrase))


@pytest.mark.parametrize("phrase", ["", ".. бот", "бот ..", "?ты бот", "бот ?ты", "бот||робот"])
def test_a_malformed_pattern_is_refused(phrase: str) -> None:
    with pytest.raises(PatternSyntaxError):
        compile_phrase(phrase)


def test_the_messenger_list_is_used_only_in_pairs() -> None:
    for name in CONTACT_CHANNELS.split(ALTERNATIVE_SEPARATOR):
        stem = name.removesuffix(STEM_SUFFIX)
        assert TriggerClass.CONTACT_EXCHANGE not in classes_of(f"просто {stem}"), name
