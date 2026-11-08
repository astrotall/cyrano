from __future__ import annotations

import ast
from pathlib import Path

from cyrano import gender_name_data
from cyrano.gender_name_data import (
    AMBIGUOUS_NAMES,
    FEMININE_ENDINGS,
    MALE_NAMES,
    MASCULINE_NAMES_ENDING_IN_A_OR_YA,
)

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "cyrano"
DATA_MODULE = "gender_name_data.py"
MIN_MALE_NAMES = 100
DATA_SETS = {
    "ambiguous": AMBIGUOUS_NAMES,
    "masculine_a_ya": MASCULINE_NAMES_ENDING_IN_A_OR_YA,
    "male": MALE_NAMES,
}
SAMPLE_NAMES = ("саша", "никита", "александр", "петр")


def test_every_data_set_is_not_empty() -> None:
    for name, names in DATA_SETS.items():
        assert names, name


def test_the_male_set_holds_at_least_a_hundred_names() -> None:
    assert len(MALE_NAMES) >= MIN_MALE_NAMES


def test_the_data_sets_do_not_overlap() -> None:
    names = list(DATA_SETS)
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            assert DATA_SETS[first].isdisjoint(DATA_SETS[second]), (first, second)


def test_every_entry_is_normalised_lowercase_cyrillic_letters_without_yo() -> None:
    for name, names in DATA_SETS.items():
        for entry in names:
            assert entry == entry.casefold(), (name, entry)
            assert "ё" not in entry, (name, entry)
            assert entry.isalpha(), (name, entry)
            assert all("а" <= character <= "я" for character in entry), (name, entry)


def test_the_exclusions_and_ambiguous_names_all_end_in_a_or_ya() -> None:
    for entry in MASCULINE_NAMES_ENDING_IN_A_OR_YA | AMBIGUOUS_NAMES:
        assert entry.endswith(FEMININE_ENDINGS), entry


def test_the_male_set_has_no_name_ending_in_a_or_ya() -> None:
    assert not [entry for entry in MALE_NAMES if entry.endswith(FEMININE_ENDINGS)]


def test_the_data_is_frozen() -> None:
    for names in DATA_SETS.values():
        assert isinstance(names, frozenset)


def test_the_samples_used_by_the_tests_are_where_they_are_expected() -> None:
    assert "саша" in AMBIGUOUS_NAMES
    assert "никита" in MASCULINE_NAMES_ENDING_IN_A_OR_YA
    assert {"александр", "петр"} <= MALE_NAMES


def test_the_name_data_lives_in_one_module() -> None:
    holders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if relative == DATA_MODULE:
            continue
        literals = {
            node.value
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if len(literals & set(SAMPLE_NAMES)) >= 2:
            holders.append(relative)

    assert holders == []
    assert {"AMBIGUOUS_NAMES", "MALE_NAMES", "MASCULINE_NAMES_ENDING_IN_A_OR_YA"} <= set(
        vars(gender_name_data)
    )


def test_only_the_guess_module_imports_the_name_data() -> None:
    importers = {
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
        if any(
            isinstance(node, ast.ImportFrom) and node.module == "cyrano.gender_name_data"
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }

    assert importers == {"services/gender_guess.py"}
