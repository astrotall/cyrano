from __future__ import annotations

import json
from datetime import time

import pytest

from cyrano.domain.errors import ContentValidationError
from cyrano.domain.schedule import (
    DEFAULT_PROACTIVE_WINDOW,
    PROACTIVE_WINDOW_RULE,
    ScheduleProfile,
    default_schedule_profile,
)
from cyrano.services.schedule_files import parse_schedule
from cyrano.services.style_files import read_import_file
from tests.style_samples import REPO_ROOT

SAMPLE_SCHEDULE = REPO_ROOT / "examples" / "schedule.sample.json"
SECRET_VALUE = "window-value-marker-zq"


def _problems(document: dict[str, object]) -> list[tuple[str, str]]:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule(json.dumps({"version": 1, **document}))
    return [(problem.path, problem.rule) for problem in caught.value.problems]


def test_the_window_defaults_to_ten_to_twenty_two() -> None:
    profile = parse_schedule('{"version": 1}')

    assert profile.proactive_window == (time(10, 0), time(22, 0)) == DEFAULT_PROACTIVE_WINDOW


@pytest.mark.parametrize(
    ("pair", "expected"),
    [
        (["00:00", "23:59"], (time(0, 0), time(23, 59))),
        (["09:30", "09:31"], (time(9, 30), time(9, 31))),
        (["12:00", "20:00"], (time(12, 0), time(20, 0))),
    ],
)
def test_a_window_with_its_start_before_its_end_is_accepted(
    pair: list[str], expected: tuple[time, time]
) -> None:
    profile = parse_schedule(json.dumps({"version": 1, "proactive_window": pair}))

    assert profile.proactive_window == expected


@pytest.mark.parametrize(
    ("pair", "path", "rule"),
    [
        (["22:00", "10:00"], "proactive_window", PROACTIVE_WINDOW_RULE),
        (["10:00", "10:00"], "proactive_window", PROACTIVE_WINDOW_RULE),
        (["10:00", "24:00"], "proactive_window[1]", "must be a time as HH:MM"),
        (["7:00", "22:00"], "proactive_window[0]", "must be a time as HH:MM"),
        (["10:00"], "proactive_window[1]", "missing"),
    ],
)
def test_a_broken_window_gives_the_path_and_the_rule(pair: list[str], path: str, rule: str) -> None:
    assert _problems({"proactive_window": pair}) == [(path, rule)]


def test_a_window_error_never_carries_the_input() -> None:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule(json.dumps({"version": 1, "proactive_window": [SECRET_VALUE, "22:00"]}))

    assert SECRET_VALUE not in str(caught.value)


def test_a_stored_profile_without_the_window_reads_with_the_default() -> None:
    stored = default_schedule_profile("UTC").stored_data()
    stored.pop("proactive_window")

    assert ScheduleProfile.model_validate(stored).proactive_window == DEFAULT_PROACTIVE_WINDOW


def test_the_window_survives_the_stored_form() -> None:
    profile = parse_schedule(json.dumps({"version": 1, "proactive_window": ["11:15", "20:45"]}))

    stored = profile.stored_data()

    assert stored["proactive_window"] == ["11:15", "20:45"]
    assert ScheduleProfile.model_validate(stored).proactive_window == (time(11, 15), time(20, 45))


def test_the_sample_file_carries_an_invented_window() -> None:
    profile = parse_schedule(read_import_file(SAMPLE_SCHEDULE))

    assert profile.proactive_window == (time(10, 0), time(21, 30))
