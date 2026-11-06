"""Pure stop-trigger detection on contact texts: normalisation, word patterns and number rules."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from cyrano.domain.enums import TriggerClass
from cyrano.domain.stop_triggers import DETECTOR_MAX_TEXT_CHARS, StopDecision
from cyrano.stop_trigger_patterns import (
    ALTERNATIVE_SEPARATOR,
    CARD_MAX_DIGITS,
    CARD_MIN_DIGITS,
    CONTACT_LINK_HOSTS,
    CURRENCY_SYMBOLS,
    DEFAULT_GAP_WORDS,
    GAP_MARK,
    HANDLE_MIN_CHARS,
    OPTIONAL_PREFIX,
    PHONE_MIN_DIGITS,
    PHONE_SEPARATOR_RUN_MAX,
    PHONE_SEPARATORS,
    SINGLE_LETTER_RUN_MIN,
    STEM_SUFFIX,
    WORD_PATTERNS,
)

NFKC: Final = "NFKC"
REMOVED_CATEGORIES = frozenset({"Mn", "Me", "Cf"})
KEPT_CATEGORY_GROUPS = frozenset({"L", "N"})
YO = "\N{CYRILLIC SMALL LETTER IO}"
YE = "\N{CYRILLIC SMALL LETTER IE}"
SPACE = " "
LETTER_DOUBLE = re.compile(r"([^\W\d_])\1")
LETTER_RUN_TO_TWO = re.compile(r"([^\W\d_])\1{2,}")
LETTER_RUN_TO_ONE = re.compile(r"([^\W\d_])\1+")
WHITESPACE_RUN = re.compile(r"\s+")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
HANDLE = re.compile(rf"@[a-z0-9_]{{{HANDLE_MIN_CHARS},}}")
CURRENCY = re.compile(f"[{re.escape(CURRENCY_SYMBOLS)}]")
CONTACT_LINK = re.compile(
    r"(?<![\w.])(?:" + "|".join(re.escape(host) for host in CONTACT_LINK_HOSTS) + r")/"
)
DIGIT_RUN = re.compile(
    rf"\+?\d(?:[{re.escape(PHONE_SEPARATORS)}]{{0,{PHONE_SEPARATOR_RUN_MAX}}}\d)+"
)
SHORT_PHONE = re.compile(r"(?<!\d)\d{3}[ \-]\d{2}[ \-]\d{2}(?!\d)")
GAP_WITH_COUNT = re.compile(rf"^{re.escape(GAP_MARK)}(\d)?$")


class PatternSyntaxError(ValueError):
    pass


def _alternative(word: str) -> str:
    if word.endswith(STEM_SUFFIX):
        return re.escape(word.removesuffix(STEM_SUFFIX)) + r"\w*"
    return re.escape(word)


def _alternatives(token: str) -> str:
    words = token.split(ALTERNATIVE_SEPARATOR)
    if not all(words):
        raise PatternSyntaxError(token)
    return "(?:" + "|".join(_alternative(word) for word in words) + ")"


def compile_phrase(phrase: str) -> str:
    tokens = phrase.split()
    if not tokens or GAP_WITH_COUNT.match(tokens[0]) or GAP_WITH_COUNT.match(tokens[-1]):
        raise PatternSyntaxError(phrase)
    if tokens[0].startswith(OPTIONAL_PREFIX) or tokens[-1].startswith(OPTIONAL_PREFIX):
        raise PatternSyntaxError(phrase)
    pieces: list[str] = []
    for token in tokens:
        gap = GAP_WITH_COUNT.match(token)
        if gap is not None:
            words = int(gap.group(1)) if gap.group(1) else DEFAULT_GAP_WORDS
            pieces.append(rf"(?:\w+ ){{0,{words}}}")
        elif token.startswith(OPTIONAL_PREFIX):
            pieces.append(f"(?:{_alternatives(token.removeprefix(OPTIONAL_PREFIX))} )?")
        else:
            pieces.append(_alternatives(token) + SPACE)
    return "(?<= )" + "".join(pieces)


def _compile_class(phrases: Iterable[str]) -> re.Pattern[str] | None:
    compiled = [f"(?:{compile_phrase(phrase)})" for phrase in phrases]
    return re.compile("|".join(compiled)) if compiled else None


def _token_without_doubles(token: str) -> str | None:
    if GAP_WITH_COUNT.match(token):
        return token
    optional = token.startswith(OPTIONAL_PREFIX)
    words = token.removeprefix(OPTIONAL_PREFIX).split(ALTERNATIVE_SEPARATOR)
    kept = [word for word in words if LETTER_DOUBLE.search(word) is None]
    if not kept:
        return "" if optional else None
    return (OPTIONAL_PREFIX if optional else "") + ALTERNATIVE_SEPARATOR.join(kept)


def _phrase_without_doubles(phrase: str) -> str | None:
    tokens: list[str] = []
    for token in phrase.split():
        kept = _token_without_doubles(token)
        if kept is None:
            return None
        if kept:
            tokens.append(kept)
    return SPACE.join(tokens)


def _without_doubles(phrases: Iterable[str]) -> list[str]:
    return [kept for phrase in phrases if (kept := _phrase_without_doubles(phrase)) is not None]


@dataclass(frozen=True, slots=True)
class _ClassPatterns:
    full: re.Pattern[str] | None
    single: re.Pattern[str] | None


def _build_patterns() -> Mapping[TriggerClass, _ClassPatterns]:
    return MappingProxyType(
        {
            trigger: _ClassPatterns(
                full=_compile_class(phrases), single=_compile_class(_without_doubles(phrases))
            )
            for trigger, phrases in WORD_PATTERNS.items()
        }
    )


CLASS_PATTERNS = _build_patterns()


@dataclass(frozen=True, slots=True)
class NormalizedText:
    base: str
    words: tuple[str, ...]
    single_letter_words: tuple[str, ...]


def _base(text: str) -> str:
    folded = unicodedata.normalize(NFKC, text[:DETECTOR_MAX_TEXT_CHARS]).casefold()
    folded = folded.replace(YO, YE)
    return "".join(c for c in folded if unicodedata.category(c) not in REMOVED_CATEGORIES)


def _words(base: str) -> str:
    kept = "".join(c if unicodedata.category(c)[0] in KEPT_CATEGORY_GROUPS else SPACE for c in base)
    return WHITESPACE_RUN.sub(SPACE, kept).strip()


def _join_spaced_letters(words: str) -> str:
    joined: list[str] = []
    run: list[str] = []
    for token in [*words.split(SPACE), ""]:
        if len(token) == 1 and token.isalpha():
            run.append(token)
            continue
        if len(run) >= SINGLE_LETTER_RUN_MIN:
            joined.append("".join(run))
        else:
            joined.extend(run)
        run = []
        if token:
            joined.append(token)
    return SPACE.join(joined)


def _padded(view: str) -> str:
    return f"{SPACE}{view}{SPACE}"


def normalize(text: str) -> NormalizedText:
    base = _base(text)
    words = _words(base)
    sources = dict.fromkeys((words, _join_spaced_letters(words)))
    two = dict.fromkeys(_padded(LETTER_RUN_TO_TWO.sub(r"\1\1", view)) for view in sources)
    one = dict.fromkeys(_padded(LETTER_RUN_TO_ONE.sub(r"\1", view)) for view in sources)
    return NormalizedText(base=base, words=tuple(two), single_letter_words=tuple(one))


def _digit_count(run: str) -> int:
    return sum(1 for character in run if character.isdecimal())


def _number_classes(base: str) -> set[TriggerClass]:
    found: set[TriggerClass] = set()
    for match in DIGIT_RUN.finditer(base):
        digits = _digit_count(match.group())
        if digits >= PHONE_MIN_DIGITS:
            found.add(TriggerClass.CONTACT_EXCHANGE)
        if CARD_MIN_DIGITS <= digits <= CARD_MAX_DIGITS:
            found.add(TriggerClass.MONEY)
    if SHORT_PHONE.search(base):
        found.add(TriggerClass.CONTACT_EXCHANGE)
    return found


def _symbol_classes(base: str) -> set[TriggerClass]:
    found: set[TriggerClass] = set()
    if EMAIL.search(base) or HANDLE.search(base) or CONTACT_LINK.search(base):
        found.add(TriggerClass.CONTACT_EXCHANGE)
    if CURRENCY.search(base):
        found.add(TriggerClass.MONEY)
    return found


def _word_classes(normalized: NormalizedText) -> set[TriggerClass]:
    found: set[TriggerClass] = set()
    for trigger, patterns in CLASS_PATTERNS.items():
        if _matches(patterns.full, normalized.words) or _matches(
            patterns.single, normalized.single_letter_words
        ):
            found.add(trigger)
    return found


def _matches(pattern: re.Pattern[str] | None, views: tuple[str, ...]) -> bool:
    return pattern is not None and any(pattern.search(view) for view in views)


def classes_of(text: str) -> frozenset[TriggerClass]:
    normalized = normalize(text)
    found = _word_classes(normalized) | _number_classes(normalized.base)
    return frozenset(found | _symbol_classes(normalized.base))


def detect(texts: str | Iterable[str]) -> StopDecision:
    items = (texts,) if isinstance(texts, str) else tuple(texts)
    found: set[TriggerClass] = set()
    for text in items:
        found |= classes_of(text)
    return StopDecision(classes=frozenset(found))
