from __future__ import annotations

import json

import pytest

from cyrano.domain.errors import ContentValidationError
from cyrano.domain.schedule import (
    DEFAULT_BUSY_LABEL,
    DEFAULT_WRAPUP_HARD_MINUTES,
    DEFAULT_WRAPUP_SOFT_MINUTES,
    MAX_BUSY_LABEL_LENGTH,
    WRAPUP_ORDER_RULE,
    ScheduleProfile,
    default_schedule_profile,
)
from cyrano.services.schedule_files import parse_schedule
from cyrano.services.style_files import read_import_file
from tests.style_samples import REPO_ROOT

SAMPLE_SCHEDULE = REPO_ROOT / "examples" / "schedule.sample.json"
SECRET_VALUE = "wrapup-value-marker-zq"
WINDOW = {"days": ["mon"], "from": "09:00", "to": "10:00"}


def _problems(document: dict[str, object]) -> list[tuple[str, str]]:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule(json.dumps({"version": 1, **document}))
    return [(problem.path, problem.rule) for problem in caught.value.problems]


def test_the_wrapup_fields_and_the_label_have_their_defaults() -> None:
    profile = parse_schedule(json.dumps({"version": 1, "busy": [WINDOW]}))

    assert profile.wrapup_soft_minutes == DEFAULT_WRAPUP_SOFT_MINUTES == 60
    assert profile.wrapup_hard_minutes == DEFAULT_WRAPUP_HARD_MINUTES == 20
    assert profile.busy[0].label == DEFAULT_BUSY_LABEL == "занят"
    assert MAX_BUSY_LABEL_LENGTH == 40


@pytest.mark.parametrize(("soft", "hard"), [(5, 1), (5, 5), (240, 240), (60, 20), (240, 1)])
def test_the_bounds_of_the_wrapup_minutes_are_accepted(soft: int, hard: int) -> None:
    profile = parse_schedule(
        json.dumps({"version": 1, "wrapup_soft_minutes": soft, "wrapup_hard_minutes": hard})
    )

    assert (profile.wrapup_soft_minutes, profile.wrapup_hard_minutes) == (soft, hard)


@pytest.mark.parametrize(
    ("document", "path", "rule"),
    [
        ({"wrapup_soft_minutes": 4}, "wrapup_soft_minutes", "must be at least 5"),
        ({"wrapup_soft_minutes": 241}, "wrapup_soft_minutes", "must be at most 240"),
        ({"wrapup_hard_minutes": 0}, "wrapup_hard_minutes", "must be at least 1"),
        (
            {"wrapup_soft_minutes": 30, "wrapup_hard_minutes": 31},
            "wrapup_hard_minutes",
            WRAPUP_ORDER_RULE,
        ),
        ({"wrapup_hard_minutes": 61}, "wrapup_hard_minutes", WRAPUP_ORDER_RULE),
        (
            {"busy": [{**WINDOW, "label": "x" * 41}]},
            "busy[0].label",
            "too long (max 40)",
        ),
    ],
)
def test_a_broken_wrapup_value_gives_the_path_and_the_rule(
    document: dict[str, object], path: str, rule: str
) -> None:
    assert _problems(document) == [(path, rule)]


@pytest.mark.parametrize("label", ["", "   "])
def test_an_empty_label_is_refused_at_its_path(label: str) -> None:
    [(path, _)] = _problems({"busy": [{**WINDOW, "label": label}]})

    assert path == "busy[0].label"


def test_a_label_is_trimmed_and_forty_characters_fit() -> None:
    profile = parse_schedule(
        json.dumps(
            {
                "version": 1,
                "busy": [{**WINDOW, "label": "  пара  "}, {**WINDOW, "label": "я" * 40}],
            }
        )
    )

    assert [window.label for window in profile.busy] == ["пара", "я" * 40]


@pytest.mark.parametrize(
    "document",
    [
        {"wrapup_soft_minutes": SECRET_VALUE},
        {"wrapup_hard_minutes": SECRET_VALUE},
        {"busy": [{**WINDOW, "label": SECRET_VALUE * 3}]},
    ],
)
def test_a_wrapup_error_never_carries_the_input(document: dict[str, object]) -> None:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule(json.dumps({"version": 1, **document}))

    assert SECRET_VALUE not in str(caught.value)


def test_a_stored_profile_without_the_new_fields_reads_with_the_defaults() -> None:
    stored = parse_schedule(json.dumps({"version": 1, "busy": [WINDOW]})).stored_data()
    stored.pop("wrapup_soft_minutes")
    stored.pop("wrapup_hard_minutes")
    busy = stored["busy"]
    assert isinstance(busy, list)
    busy[0].pop("label")

    profile = ScheduleProfile.model_validate(stored)

    assert (profile.wrapup_soft_minutes, profile.wrapup_hard_minutes) == (60, 20)
    assert profile.busy[0].label == "занят"


def test_the_new_fields_survive_the_stored_form() -> None:
    profile = parse_schedule(
        json.dumps(
            {
                "version": 1,
                "wrapup_soft_minutes": 90,
                "wrapup_hard_minutes": 10,
                "busy": [{**WINDOW, "label": "лекция"}],
            }
        )
    )

    assert ScheduleProfile.model_validate(profile.stored_data()) == profile
    assert default_schedule_profile("UTC").stored_data()["wrapup_soft_minutes"] == 60


def test_the_sample_file_carries_invented_wrapup_values_and_a_label() -> None:
    profile = parse_schedule(read_import_file(SAMPLE_SCHEDULE))

    assert (profile.wrapup_soft_minutes, profile.wrapup_hard_minutes) == (45, 15)
    assert profile.busy[0].label == "на паре"
