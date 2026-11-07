from __future__ import annotations

import json
from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from cyrano.domain.enums import Weekday
from cyrano.domain.errors import ContentValidationError, ImportFileError, ScheduleTimezoneUnresolved
from cyrano.domain.schedule import (
    MAX_BUSY_WINDOWS,
    MAX_SPLIT_PAUSE_SECONDS,
    MAX_TYPING_SECONDS,
    ScheduleProfile,
    default_schedule_profile,
)
from cyrano.services.schedule_files import (
    MAX_SCHEDULE_FILE_BYTES,
    parse_schedule,
)
from cyrano.services.style_files import read_import_file
from tests.style_samples import REPO_ROOT

SAMPLE_SCHEDULE = REPO_ROOT / "examples" / "schedule.sample.json"
SECRET_VALUE = "schedule-value-marker-zq"


def _problems(document: dict[str, object]) -> list[tuple[str, str]]:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule(json.dumps({"version": 1, **document}))
    return [(problem.path, problem.rule) for problem in caught.value.problems]


def test_every_field_but_the_version_has_its_default() -> None:
    profile = parse_schedule('{"version": 1}')

    assert profile.timezone is None
    assert profile.night.start == (time(1, 0), time(2, 0))
    assert profile.night.end_weekday == (time(9, 0), time(9, 0))
    assert profile.night.end_weekend == (time(11, 0), time(12, 0))
    assert profile.busy == ()
    assert profile.reaction.ongoing_seconds == (1, 4)
    assert profile.reaction.new_seconds == (20, 120)
    assert profile.reaction.ongoing_window_minutes == 10
    assert profile.read_pause_seconds == (1, 3)
    assert profile.typing.chars_per_second == 5
    assert profile.typing.min_seconds == 2
    assert profile.typing.max_seconds == 25
    assert profile.postponed_spread_minutes == (1, 15)
    assert profile.split_pause_seconds == (3, 8)


def test_the_version_is_required() -> None:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule("{}")

    assert [(p.path, p.rule) for p in caught.value.problems] == [("version", "missing")]


@pytest.mark.parametrize("version", [0, 2, "1", True, 1.0])
def test_only_version_one_is_accepted(version: object) -> None:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule(json.dumps({"version": version}))

    assert caught.value.problems[0].path == "version"


def test_the_built_in_profile_is_all_defaults_with_the_given_timezone() -> None:
    profile = default_schedule_profile("Europe/Moscow")

    assert profile.timezone == "Europe/Moscow"
    assert profile.busy == ()
    assert profile == parse_schedule('{"version": 1, "timezone": "Europe/Moscow"}')


def test_a_valid_full_profile_round_trips_through_its_stored_form() -> None:
    document = {
        "version": 1,
        "timezone": "Asia/Tokyo",
        "night": {
            "start": ["00:00", "05:00"],
            "end_weekday": ["05:01", "07:00"],
            "end_weekend": ["10:00", "10:30"],
        },
        "busy": [
            {"days": ["sun", "mon", "mon"], "from": "09:00", "to": "09:01"},
        ],
        "reaction": {
            "ongoing_seconds": [0, 0],
            "new_seconds": [5, 3600],
            "ongoing_window_minutes": 120,
        },
        "read_pause_seconds": [0, 10],
        "typing": {"chars_per_second": 50, "min_seconds": 0, "max_seconds": MAX_TYPING_SECONDS},
        "postponed_spread_minutes": [0, 120],
        "split_pause_seconds": [0, MAX_SPLIT_PAUSE_SECONDS],
    }

    profile = parse_schedule(json.dumps(document))

    assert profile.busy[0].days == (Weekday.MON, Weekday.SUN)
    assert profile.busy[0].start == time(9, 0)
    assert ScheduleProfile.model_validate(profile.stored_data()) == profile
    assert profile.stored_data()["busy"] == [
        {"days": ["mon", "sun"], "from": "09:00", "to": "09:01"}
    ]
    assert profile.stored_data()["night"] == document["night"]


ONE_BUSY_WINDOW = {"days": ["mon"], "from": "09:00", "to": "10:00"}


def _busy(**changes: object) -> dict[str, object]:
    return {"busy": [{**ONE_BUSY_WINDOW, **changes}]}


RULE_CASES: list[tuple[dict[str, object], str, str]] = [
    ({"timezone": "Mars/Olympus"}, "timezone", "must be an IANA time zone name"),
    ({"timezone": ""}, "timezone", "must be an IANA time zone name"),
    ({"timezone": "../etc/passwd"}, "timezone", "must be an IANA time zone name"),
    ({"timezone": 5}, "timezone", "must be text"),
    ({"night": 1}, "night", "must be an object"),
    ({"night": {"start": ["1:00", "02:00"]}}, "night.start[0]", "must be a time as HH:MM"),
    ({"night": {"start": ["01:00", "24:00"]}}, "night.start[1]", "must be a time as HH:MM"),
    ({"night": {"start": ["01:00", "02:60"]}}, "night.start[1]", "must be a time as HH:MM"),
    ({"night": {"start": ["01:00", 200]}}, "night.start[1]", "must be a time as HH:MM"),
    ({"night": {"start": "01:00"}}, "night.start", "must be a list"),
    ({"night": {"start": ["01:00"]}}, "night.start[1]", "missing"),
    ({"night": {"start": ["01:00", "02:00", "03:00"]}}, "night.start", "too many (max 2)"),
    ({"night": {"start": ["02:00", "01:00"]}}, "night.start", "must be [lo, hi] with lo <= hi"),
    (
        {"night": {"end_weekday": ["10:00", "09:00"]}},
        "night.end_weekday",
        "must be [lo, hi] with lo <= hi",
    ),
    (
        {"night": {"end_weekend": ["12:00", "11:00"]}},
        "night.end_weekend",
        "must be [lo, hi] with lo <= hi",
    ),
    ({"night": {"start": ["04:00", "05:01"]}}, "night", "the start must not be after 05:00"),
    (
        {"night": {"start": ["01:00", "02:00"], "end_weekday": ["02:00", "09:00"]}},
        "night",
        "every end must be after the latest start",
    ),
    (
        {"night": {"start": ["01:00", "02:00"], "end_weekend": ["01:30", "09:00"]}},
        "night",
        "every end must be after the latest start",
    ),
    (_busy(days=[]), "busy[0].days", "must name at least one day"),
    (
        _busy(days=["monday"]),
        "busy[0].days[0]",
        "must be 'mon', 'tue', 'wed', 'thu', 'fri', 'sat' or 'sun'",
    ),
    (_busy(days="mon"), "busy[0].days", "must be a list"),
    (_busy(**{"from": "10:00", "to": "10:00"}), "busy[0]", "from must be before to"),
    (_busy(**{"from": "11:00", "to": "10:00"}), "busy[0]", "from must be before to"),
    (_busy(**{"from": "9:00"}), "busy[0].from", "must be a time as HH:MM"),
    (_busy(to="ten"), "busy[0].to", "must be a time as HH:MM"),
    ({"busy": 3}, "busy", "must be a list"),
    (
        {"busy": [ONE_BUSY_WINDOW] * (MAX_BUSY_WINDOWS + 1)},
        "busy",
        f"too many (max {MAX_BUSY_WINDOWS})",
    ),
    (
        {"reaction": {"ongoing_seconds": [4, 1]}},
        "reaction.ongoing_seconds",
        "must be [lo, hi] with lo <= hi",
    ),
    (
        {"reaction": {"new_seconds": [120, 20]}},
        "reaction.new_seconds",
        "must be [lo, hi] with lo <= hi",
    ),
    (
        {"reaction": {"ongoing_seconds": [-1, 4]}},
        "reaction.ongoing_seconds[0]",
        "must be at least 0",
    ),
    ({"reaction": {"new_seconds": [1, 3601]}}, "reaction.new_seconds[1]", "must be at most 3600"),
    (
        {"reaction": {"ongoing_seconds": [1.5, 4]}},
        "reaction.ongoing_seconds[0]",
        "must be an integer",
    ),
    (
        {"reaction": {"ongoing_window_minutes": 0}},
        "reaction.ongoing_window_minutes",
        "must be at least 1",
    ),
    (
        {"reaction": {"ongoing_window_minutes": 121}},
        "reaction.ongoing_window_minutes",
        "must be at most 120",
    ),
    (
        {"reaction": {"ongoing_window_minutes": True}},
        "reaction.ongoing_window_minutes",
        "must be an integer",
    ),
    ({"read_pause_seconds": [3, 1]}, "read_pause_seconds", "must be [lo, hi] with lo <= hi"),
    ({"read_pause_seconds": [1, 11]}, "read_pause_seconds[1]", "must be at most 10"),
    ({"typing": {"chars_per_second": 0}}, "typing.chars_per_second", "must be at least 1"),
    ({"typing": {"chars_per_second": 51}}, "typing.chars_per_second", "must be at most 50"),
    (
        {"typing": {"min_seconds": 30, "max_seconds": 25}},
        "typing",
        "min_seconds must not be above max_seconds",
    ),
    ({"typing": {"max_seconds": 61}}, "typing.max_seconds", "must be at most 60"),
    ({"typing": {"min_seconds": -1}}, "typing.min_seconds", "must be at least 0"),
    (
        {"postponed_spread_minutes": [15, 1]},
        "postponed_spread_minutes",
        "must be [lo, hi] with lo <= hi",
    ),
    ({"postponed_spread_minutes": [1, 121]}, "postponed_spread_minutes[1]", "must be at most 120"),
    ({"split_pause_seconds": [8, 3]}, "split_pause_seconds", "must be [lo, hi] with lo <= hi"),
    ({"split_pause_seconds": [3, 61]}, "split_pause_seconds[1]", "must be at most 60"),
    ({"split_pause_seconds": [-1, 3]}, "split_pause_seconds[0]", "must be at least 0"),
    ({"split_pause_seconds": [1.5, 3]}, "split_pause_seconds[0]", "must be an integer"),
]


@pytest.mark.parametrize(
    ("document", "path", "rule"), RULE_CASES, ids=[f"{case[1]}:{case[2]}" for case in RULE_CASES]
)
def test_every_broken_rule_gives_the_json_path_and_the_rule(
    document: dict[str, object], path: str, rule: str
) -> None:
    assert _problems(document) == [(path, rule)]


@pytest.mark.parametrize(
    ("document", "path"),
    [
        ({SECRET_VALUE: 1}, "(file)"),
        ({"night": {SECRET_VALUE: 1}}, "night"),
        ({"busy": [{"days": ["mon"], "from": "09:00", "to": "10:00", SECRET_VALUE: 1}]}, "busy[0]"),
        ({"reaction": {SECRET_VALUE: 1}}, "reaction"),
        ({"typing": {SECRET_VALUE: 1}}, "typing"),
    ],
)
def test_an_unknown_key_is_refused_at_its_parent_without_being_named(
    document: dict[str, object], path: str
) -> None:
    assert _problems(document) == [(path, "unknown key")]


@pytest.mark.parametrize(
    "document",
    [
        {"timezone": SECRET_VALUE},
        {"night": {"start": [SECRET_VALUE, "02:00"]}},
        {"busy": [{"days": [SECRET_VALUE], "from": "09:00", "to": "10:00"}]},
        {"busy": [{"days": ["mon"], "from": SECRET_VALUE, "to": "10:00"}]},
        {"read_pause_seconds": [SECRET_VALUE, 3]},
        {"split_pause_seconds": [SECRET_VALUE, 3]},
    ],
)
def test_a_validation_error_never_carries_the_input(document: dict[str, object]) -> None:
    with pytest.raises(ContentValidationError) as caught:
        parse_schedule(json.dumps({"version": 1, **document}))

    assert SECRET_VALUE not in str(caught.value)


def test_all_the_problems_of_a_file_are_reported_together() -> None:
    problems = _problems(
        {"timezone": "Mars/Olympus", "read_pause_seconds": [3, 1], "typing": {"max_seconds": 99}}
    )

    assert {path for path, _ in problems} == {
        "timezone",
        "read_pause_seconds",
        "typing.max_seconds",
    }


@pytest.mark.parametrize("timezone", ["UTC", "Europe/Moscow", "America/New_York", "Asia/Kolkata"])
def test_real_timezones_are_accepted(timezone: str) -> None:
    assert parse_schedule(json.dumps({"version": 1, "timezone": timezone})).timezone == timezone


def test_a_null_timezone_means_the_default() -> None:
    assert parse_schedule('{"version": 1, "timezone": null}').timezone is None


def test_the_timezone_is_filled_only_when_the_file_has_none() -> None:
    unset = parse_schedule('{"version": 1}')
    set_explicitly = parse_schedule('{"version": 1, "timezone": "Asia/Tokyo"}')

    assert unset.with_default_timezone("Europe/Moscow").timezone == "Europe/Moscow"
    assert set_explicitly.with_default_timezone("Europe/Moscow").timezone == "Asia/Tokyo"


def test_a_profile_without_a_timezone_has_no_zone_instead_of_a_silent_utc() -> None:
    with pytest.raises(ScheduleTimezoneUnresolved):
        _ = parse_schedule('{"version": 1}').zone


def test_the_profile_is_frozen() -> None:
    with pytest.raises(ValidationError):
        default_schedule_profile("UTC").version = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    ("raw", "rule_part"),
    [
        ("not json", "invalid JSON"),
        ("", "invalid JSON"),
        ("[1, 2]", "must be an object"),
        ("null", "must be an object"),
        ("7", "must be an object"),
    ],
)
def test_a_document_that_is_not_an_object_is_readable(raw: str, rule_part: str) -> None:
    with pytest.raises(ContentValidationError, match=rule_part):
        parse_schedule(raw)


def test_a_deeply_nested_document_is_readable() -> None:
    with pytest.raises(ContentValidationError):
        parse_schedule("[" * 200_000)


def test_the_sample_file_validates_and_holds_invented_generic_values() -> None:
    profile = parse_schedule(read_import_file(SAMPLE_SCHEDULE))

    assert profile.night.start == (time(0, 30), time(1, 30))
    assert profile.night.end_weekday == (time(8, 0), time(8, 0))
    assert profile.night.end_weekend == (time(10, 0), time(11, 0))
    assert [(w.days, w.start, w.end) for w in profile.busy] == [
        ((Weekday.TUE, Weekday.THU), time(9, 0), time(10, 30))
    ]
    assert profile.split_pause_seconds == (3, 8)


def test_the_split_pause_limit_is_one_minute() -> None:
    assert MAX_SPLIT_PAUSE_SECONDS == 60


def test_a_stored_profile_without_the_split_pause_reads_with_the_default() -> None:
    stored = default_schedule_profile("UTC").stored_data()
    stored.pop("split_pause_seconds")

    profile = ScheduleProfile.model_validate(stored)

    assert profile.split_pause_seconds == (3, 8)


def test_the_file_size_limit_is_exact(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    body = '{"version": 1}'
    path.write_text(body + " " * (MAX_SCHEDULE_FILE_BYTES - len(body)))
    assert read_import_file(path, max_bytes=MAX_SCHEDULE_FILE_BYTES)

    path.write_text(body + " " * (MAX_SCHEDULE_FILE_BYTES - len(body) + 1))
    with pytest.raises(ImportFileError, match="larger than"):
        read_import_file(path, max_bytes=MAX_SCHEDULE_FILE_BYTES)
