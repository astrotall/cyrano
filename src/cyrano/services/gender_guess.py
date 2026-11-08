from __future__ import annotations

import re
import unicodedata
from typing import Final

from cyrano.domain.enums import ContactGender
from cyrano.gender_name_data import (
    AMBIGUOUS_NAMES,
    FEMININE_ENDINGS,
    MALE_NAMES,
    MASCULINE_NAMES_ENDING_IN_A_OR_YA,
    MIN_NAME_LETTERS,
    NAME_LETTERS_PATTERN,
    YE_LETTER,
    YO_LETTER,
)

NAME_LETTERS: Final = re.compile(NAME_LETTERS_PATTERN)
NORMAL_FORM: Final = "NFC"


def _first_word(name: str) -> str:
    words = name.split()
    return words[0] if words else ""


def _normalised(name: str | None) -> str | None:
    if not name:
        return None
    word = (
        unicodedata.normalize(NORMAL_FORM, _first_word(name))
        .casefold()
        .replace(YO_LETTER, YE_LETTER)
    )
    letters = "".join(character for character in word if character.isalpha())
    if len(letters) < MIN_NAME_LETTERS or NAME_LETTERS.fullmatch(letters) is None:
        return None
    return letters


def guess_gender(name: str | None) -> ContactGender | None:
    letters = _normalised(name)
    if letters is None or letters in AMBIGUOUS_NAMES:
        return None
    if letters in MASCULINE_NAMES_ENDING_IN_A_OR_YA or letters in MALE_NAMES:
        return ContactGender.MALE
    if letters.endswith(FEMININE_ENDINGS):
        return ContactGender.FEMALE
    return None
