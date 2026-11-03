from __future__ import annotations

import pytest

from tests import privacy
from tests.privacy import MIN_ASSERTED_MARKER_DIGITS, MIN_MARKER_DIGITS, assert_absent

MARKER_NAMES = sorted(name for name in vars(privacy) if name.startswith("FAKE_"))


def test_there_are_marker_constants() -> None:
    assert MARKER_NAMES


@pytest.mark.parametrize("name", MARKER_NAMES)
def test_every_marker_constant_has_at_least_twelve_digits(name: str) -> None:
    digits = "".join(character for character in str(getattr(privacy, name)) if character.isdigit())

    assert len(digits) >= MIN_MARKER_DIGITS


def test_marker_constants_are_distinct() -> None:
    values = [str(getattr(privacy, name)) for name in MARKER_NAMES]

    assert len(set(values)) == len(values)


@pytest.mark.parametrize("marker", ["7001", 7001, "123456789", 987654321, "+155500012"])
def test_assert_absent_refuses_a_short_numeric_marker(marker: str | int) -> None:
    with pytest.raises(AssertionError, match="shorter than"):
        assert_absent(marker, "nothing here")


@pytest.mark.parametrize("marker", ["1234567890", 1234567890, "+15550001234"])
def test_assert_absent_accepts_a_numeric_marker_of_ten_digits(marker: str | int) -> None:
    assert MIN_ASSERTED_MARKER_DIGITS == 10

    assert_absent(marker, "nothing here")


def test_assert_absent_passes_when_the_marker_is_missing() -> None:
    assert_absent(privacy.FAKE_TELEGRAM_ID, "created 1767225600.0170019 msecs 17")


def test_assert_absent_fails_when_the_marker_is_present() -> None:
    with pytest.raises(AssertionError, match="found"):
        assert_absent(privacy.FAKE_TELEGRAM_ID, f"user {privacy.FAKE_TELEGRAM_ID} wrote")


def test_assert_absent_does_not_apply_the_length_rule_to_text_markers() -> None:
    assert_absent("secret-7001", "nothing here")
    with pytest.raises(AssertionError, match="found"):
        assert_absent("secret-7001", "has secret-7001 inside")
