from __future__ import annotations

import json

import pytest

from cyrano.domain.errors import ContentValidationError
from cyrano.domain.schedule import (
    DEFAULT_QUOTE_LATE_SECONDS,
    MAX_QUOTE_LATE_SECONDS,
    ScheduleProfile,
    default_schedule_profile,
)
from cyrano.services.schedule_files import parse_schedule
from cyrano.services.style_files import read_import_file
from tests.style_samples import REPO_ROOT

SAMPLE_SCHEDULE = REPO_ROOT / "examples" / "schedule.sample.json"
SECRET_VALUE = "quote-late-value-marker-zq"
FIELD = "quote_late_seconds"


def _problems(value: object) -> list[tuple[str, str]]:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule(json.dumps({"version": 1, FIELD: value}))
    return [(problem.path, problem.rule) for problem in caught.value.problems]


def test_the_default_is_five_minutes() -> None:
    profile = parse_schedule(json.dumps({"version": 1}))

    assert profile.quote_late_seconds == DEFAULT_QUOTE_LATE_SECONDS == 300
    assert default_schedule_profile("UTC").quote_late_seconds == 300
    assert MAX_QUOTE_LATE_SECONDS == 86400


@pytest.mark.parametrize("value", [0, 1, 300, 86400])
def test_the_bounds_are_accepted(value: int) -> None:
    assert parse_schedule(json.dumps({"version": 1, FIELD: value})).quote_late_seconds == value


@pytest.mark.parametrize(
    ("value", "rule"),
    [(-1, "must be at least 0"), (86401, "must be at most 86400")],
)
def test_a_value_out_of_range_names_only_the_path_and_the_rule(value: int, rule: str) -> None:
    assert _problems(value) == [(FIELD, rule)]


@pytest.mark.parametrize("value", [SECRET_VALUE, 1.5, True, None, [1]])
def test_a_value_that_is_not_an_integer_is_refused_at_its_path(value: object) -> None:
    [(path, _)] = _problems(value)

    assert path == FIELD


def test_an_error_never_carries_the_input() -> None:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule(json.dumps({"version": 1, FIELD: SECRET_VALUE}))

    assert SECRET_VALUE not in str(caught.value)


def test_a_stored_profile_without_the_field_reads_with_the_default() -> None:
    stored = parse_schedule(json.dumps({"version": 1, FIELD: 60})).stored_data()
    stored.pop(FIELD)

    assert ScheduleProfile.model_validate(stored).quote_late_seconds == 300


def test_the_field_survives_the_stored_form() -> None:
    profile = parse_schedule(json.dumps({"version": 1, FIELD: 0}))

    assert ScheduleProfile.model_validate(profile.stored_data()) == profile


def test_the_sample_file_carries_an_invented_value() -> None:
    assert parse_schedule(read_import_file(SAMPLE_SCHEDULE)).quote_late_seconds == 240
