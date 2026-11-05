from __future__ import annotations

import pytest
from sqlalchemy.engine import URL

from cyrano.db.repositories import SqlScheduleProfileRepository
from cyrano.domain.errors import StoredScheduleInvalid
from cyrano.domain.schedule import ScheduleProfile
from tests.db.conftest import execute, fetch_all

FIRST = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": "Asia/Tokyo",
        "busy": [{"days": ["tue", "mon"], "from": "09:00", "to": "10:00"}],
    }
)
SECOND = ScheduleProfile.model_validate({"version": 1, "read_pause_seconds": [2, 2]})


async def test_nothing_is_stored_on_a_fresh_database(
    schedule_profiles: SqlScheduleProfileRepository, migrated_url: URL
) -> None:
    assert await schedule_profiles.get() is None
    assert await fetch_all(migrated_url, "SELECT id FROM schedule_profile") == []


async def test_a_replaced_profile_is_read_back_unchanged(
    schedule_profiles: SqlScheduleProfileRepository,
) -> None:
    returned = await schedule_profiles.replace(FIRST)

    assert returned == FIRST
    assert await schedule_profiles.get() == FIRST


async def test_replace_keeps_one_row_and_the_last_profile(
    schedule_profiles: SqlScheduleProfileRepository, migrated_url: URL
) -> None:
    await schedule_profiles.replace(FIRST)
    await schedule_profiles.replace(SECOND)

    assert await schedule_profiles.get() == SECOND
    assert await fetch_all(migrated_url, "SELECT id FROM schedule_profile") == [(1,)]


async def test_replace_moves_updated_at(
    schedule_profiles: SqlScheduleProfileRepository, migrated_url: URL
) -> None:
    await schedule_profiles.replace(FIRST)
    await execute(migrated_url, "UPDATE schedule_profile SET updated_at = now() - interval '1 day'")
    before = await fetch_all(migrated_url, "SELECT updated_at FROM schedule_profile")

    await schedule_profiles.replace(SECOND)

    after = await fetch_all(migrated_url, "SELECT updated_at FROM schedule_profile")
    assert after != before


async def test_the_row_stores_json_keys_of_the_file_format(
    schedule_profiles: SqlScheduleProfileRepository, migrated_url: URL
) -> None:
    await schedule_profiles.replace(FIRST)

    rows = await fetch_all(
        migrated_url,
        "SELECT data -> 'busy' -> 0 ->> 'from', data ->> 'version' FROM schedule_profile",
    )

    assert rows == [("09:00", "1")]


async def test_a_corrupt_stored_profile_raises_a_domain_error_without_the_data(
    schedule_profiles: SqlScheduleProfileRepository, migrated_url: URL
) -> None:
    await execute(
        migrated_url,
        "INSERT INTO schedule_profile (id, data) "
        'VALUES (1, \'{"version": 99, "marker": "corrupt-marker-zq"}\'::jsonb)',
    )

    with pytest.raises(StoredScheduleInvalid) as caught:
        await schedule_profiles.get()

    assert "corrupt-marker-zq" not in str(caught.value)
    assert caught.value.__cause__ is None
