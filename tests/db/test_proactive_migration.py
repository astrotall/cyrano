from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from cyrano.domain.enums import OutboxSource, OutboxState
from tests.db.conftest import execute, fetch_all
from tests.privacy import FAKE_TELEGRAM_ID

SOURCE_CHECK = "ck_scheduled_messages_source_allowed"
DUE = "2026-11-04 12:00:00+00"


async def _column(url: URL) -> list[tuple[object, ...]]:
    return await fetch_all(
        url,
        "SELECT is_nullable, data_type FROM information_schema.columns "
        "WHERE table_name = 'contacts' AND column_name = 'proactive_not_before'",
    )


async def _source_check(url: URL) -> str:
    [(definition,)] = await fetch_all(
        url,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "  # noqa: S608
        f"WHERE conname = '{SOURCE_CHECK}'",
    )
    return str(definition)


async def _contact(url: URL) -> None:
    await execute(
        url,
        "INSERT INTO contacts (id, tg_id, status) "  # noqa: S608
        f"VALUES (1, {FAKE_TELEGRAM_ID}, 'active')",
    )


async def _row(url: URL, row_id: int, source: str, state: str) -> None:
    await execute(
        url,
        "INSERT INTO scheduled_messages "  # noqa: S608
        "(id, contact_id, trigger_message_id, text, source, send_at, due_at, state) "
        f"VALUES ({row_id}, 1, {row_id}, 'invented row text', '{source}', "
        f"'{DUE}', '{DUE}', '{state}')",
    )


async def test_revision_0013_adds_the_column_and_lets_the_check_accept_proactive(
    database_url: URL, alembic_config: Config
) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config, "0012")
    await _contact(database_url)
    with pytest.raises(DBAPIError, match=SOURCE_CHECK):
        await _row(database_url, 1, "proactive", "scheduled")

    await asyncio.to_thread(command.upgrade, alembic_config, "0013")
    await _row(database_url, 1, "proactive", "scheduled")

    assert await _column(database_url) == [("YES", "timestamp with time zone")]
    assert await fetch_all(database_url, "SELECT proactive_not_before FROM contacts") == [(None,)]
    assert "'proactive'" in await _source_check(database_url)


async def test_the_check_matches_the_sources_of_the_domain(migrated_url: URL) -> None:
    definition = await _source_check(migrated_url)

    for source in OutboxSource:
        assert f"'{source.value}'" in definition


async def test_the_check_still_refuses_an_unknown_source(migrated_url: URL) -> None:
    await _contact(migrated_url)

    with pytest.raises(DBAPIError, match=SOURCE_CHECK):
        await _row(migrated_url, 1, "invented_source", "scheduled")


async def test_the_downgrade_cancels_waiting_proactive_rows_and_keeps_every_row(
    database_url: URL, alembic_config: Config
) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config, "0013")
    await _contact(database_url)
    await execute(
        database_url,
        f"UPDATE contacts SET proactive_not_before = '{DUE}'",  # noqa: S608
    )
    states = [state.value for state in OutboxState]
    for number, state in enumerate(states, start=1):
        await _row(database_url, number, "proactive", state)
    await _row(database_url, 10, "autopilot", "scheduled")
    await _row(database_url, 11, "manual_test", "scheduled")

    await asyncio.to_thread(command.downgrade, alembic_config, "0012")

    rows = await fetch_all(
        database_url,
        "SELECT id, source, state, cancel_reason FROM scheduled_messages ORDER BY id",
    )
    expected = [
        (
            number,
            "autopilot",
            "cancelled" if state == "scheduled" else state,
            "downgrade" if state == "scheduled" else None,
        )
        for number, state in enumerate(states, start=1)
    ]
    assert rows == [
        *expected,
        (10, "autopilot", "scheduled", None),
        (11, "manual_test", "scheduled", None),
    ]
    assert await _column(database_url) == []
    assert "'proactive'" not in await _source_check(database_url)

    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    assert await fetch_all(database_url, "SELECT proactive_not_before FROM contacts") == [(None,)]
