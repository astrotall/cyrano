"""Parsing of Leomatch profile texts. The card format is an assumption, see decisions.md."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cyrano.domain.enums import LeomatchTextKind
from cyrano.domain.models import (
    MAX_LEOMATCH_AGE,
    MAX_LEOMATCH_CITY_LENGTH,
    MAX_LEOMATCH_NAME_LENGTH,
    MAX_LEOMATCH_RELATED_TEXTS,
    MAX_LEOMATCH_TEXT_LENGTH,
    MIN_LEOMATCH_AGE,
    LeomatchProfile,
    LeomatchSourceText,
)
from cyrano.services.sanitize import clean_text

HYPHEN = "-"
EN_DASH = "\N{EN DASH}"
EM_DASH = "\N{EM DASH}"
DASH_SEPARATOR = re.compile(rf"\s[{HYPHEN}{EN_DASH}{EM_DASH}]\s")
AGE_TEXT = re.compile(r"[0-9]{2}")
FIELD_SEPARATOR = ","
LINE_BREAK = "\n"
NAME_AND_AGE_PARTS = 2
NAME_AGE_AND_CITY_PARTS = 3


@dataclass(frozen=True, slots=True)
class LeomatchFields:
    name: str | None = None
    age: int | None = None
    city: str | None = None
    about: str | None = None

    @property
    def matched(self) -> bool:
        return self.name is not None


NO_FIELDS = LeomatchFields()


def _age_of(text: str) -> int | None:
    if AGE_TEXT.fullmatch(text) is None:
        return None
    age = int(text)
    return age if MIN_LEOMATCH_AGE <= age <= MAX_LEOMATCH_AGE else None


def parse_leomatch_text(text: str) -> LeomatchFields:
    cleaned = clean_text(text, MAX_LEOMATCH_TEXT_LENGTH)
    if cleaned is None:
        return NO_FIELDS
    separator = DASH_SEPARATOR.search(cleaned)
    if separator is None:
        return NO_FIELDS
    head = cleaned[: separator.start()]
    about = cleaned[separator.end() :].strip()
    if LINE_BREAK in head or not about:
        return NO_FIELDS
    parts = [part.strip() for part in head.split(FIELD_SEPARATOR)]
    if len(parts) not in {NAME_AND_AGE_PARTS, NAME_AGE_AND_CITY_PARTS}:
        return NO_FIELDS
    name, age_text, *rest = parts
    age = _age_of(age_text)
    city = rest[0] if rest else None
    if not name or len(name) > MAX_LEOMATCH_NAME_LENGTH or age is None:
        return NO_FIELDS
    if rest and (not city or len(city) > MAX_LEOMATCH_CITY_LENGTH):
        return NO_FIELDS
    return LeomatchFields(name=name, age=age, city=city, about=about)


def _first_text(texts: Sequence[LeomatchSourceText], kind: LeomatchTextKind) -> str | None:
    for item in texts:
        if item.kind is kind:
            cleaned = clean_text(item.text, MAX_LEOMATCH_TEXT_LENGTH)
            if cleaned is not None:
                return cleaned
    return None


def build_leomatch_profile(texts: Sequence[LeomatchSourceText]) -> LeomatchProfile | None:
    card = _first_text(texts, LeomatchTextKind.CARD)
    match = _first_text(texts, LeomatchTextKind.MATCH)
    raw_text, other = (card, match) if card is not None else (match, None)
    if raw_text is None:
        return None
    fields = NO_FIELDS
    for candidate in (card, match):
        if candidate is not None and (parsed := parse_leomatch_text(candidate)).matched:
            fields = parsed
            break
    related = [other] if other is not None else []
    return LeomatchProfile(
        raw_text=raw_text,
        related_texts=related[:MAX_LEOMATCH_RELATED_TEXTS],
        name=fields.name,
        age=fields.age,
        city=fields.city,
        about=fields.about,
    )
