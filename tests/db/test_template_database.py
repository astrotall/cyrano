from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable

import pytest
from sqlalchemy import pool
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from tests.db import conftest as database_fixtures
from tests.db.conftest import (
    TemplateDatabase,
    build_template,
    cloned_database,
    create_database,
    drop_database,
    drop_stale_templates,
    execute,
    fetch_all,
    list_database_names,
    read_admin_url,
    wait_until_unused,
)
from tests.db.template import (
    CLONE_NAME_PATTERN,
    CLONE_PREFIX,
    MAX_IDENTIFIER_LENGTH,
    STALE_AFTER_SECONDS,
    TEMPLATE_PREFIX,
    WORKER_ENV,
    clone_name,
    stale_templates,
    template_epoch,
    template_name,
)
from tests.privacy import FAKE_TELEGRAM_ID

EPOCH = 1_800_000_000
PID = 4242
WORKER = "gw3"
LATER_TEMPLATE_OFFSET = 3600
INSERT_CONTACT = f"INSERT INTO contacts (tg_id, status) VALUES ({FAKE_TELEGRAM_ID}, 'new')"  # noqa: S608
SCHEMA_QUERIES = {
    "tables": (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name"
    ),
    "columns": (
        "SELECT table_name, column_name, ordinal_position, data_type, udt_name, is_nullable, "
        "column_default, character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position"
    ),
    "constraints": (
        "SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
    ),
    "indexes": (
        "SELECT tablename, indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' ORDER BY tablename, indexname"
    ),
    "enums": (
        "SELECT t.typname, e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "ORDER BY t.typname, e.enumsortorder"
    ),
    "modes": (
        "SELECT name, prompt_fragment, few_shot_examples::text, enabled FROM modes ORDER BY id"
    ),
    "revision": "SELECT version_num FROM alembic_version",
}


class RecordingRequest:
    def __init__(self) -> None:
        self.finalizers: list[Callable[[], object]] = []

    def addfinalizer(self, finalizer: Callable[[], object]) -> None:
        self.finalizers.append(finalizer)

    def finish(self) -> None:
        for finalizer in reversed(self.finalizers):
            finalizer()


async def _snapshot(url: URL) -> dict[str, list[tuple[object, ...]]]:
    snapshot = {name: await fetch_all(url, query) for name, query in SCHEMA_QUERIES.items()}
    for [table] in snapshot["tables"]:
        snapshot[f"rows of {table}"] = await fetch_all(
            url,
            f"SELECT count(*) FROM {table}",  # noqa: S608
        )
    return snapshot


async def _connections(admin_url: URL, name: str) -> int:
    [(count,)] = await fetch_all(
        admin_url,
        "SELECT count(*) FROM pg_stat_activity WHERE datname = :name",
        {"name": name},
    )
    return int(str(count))


def test_a_template_name_carries_the_prefix_the_time_the_process_and_the_worker() -> None:
    assert template_name(EPOCH, PID, WORKER) == f"{TEMPLATE_PREFIX}{EPOCH}_{PID}_{WORKER}"
    assert template_name(EPOCH, PID, None) == f"{TEMPLATE_PREFIX}{EPOCH}_{PID}"


def test_a_clone_name_carries_its_own_prefix_and_never_repeats() -> None:
    names = {clone_name(EPOCH, PID, WORKER) for _ in range(50)}

    assert len(names) == 50
    assert all(CLONE_NAME_PATTERN.match(name) for name in names)
    assert all(name.startswith(f"{CLONE_PREFIX}{EPOCH}_{PID}_{WORKER}_") for name in names)
    assert all(template_epoch(name) is None for name in names)


def test_every_name_stays_within_the_identifier_limit() -> None:
    assert len(template_name(EPOCH, PID, WORKER)) <= MAX_IDENTIFIER_LENGTH
    assert len(clone_name(EPOCH, 9_999_999, "gw999999")) <= MAX_IDENTIFIER_LENGTH
    with pytest.raises(ValueError, match="longer"):
        template_name(EPOCH, PID, "w" * MAX_IDENTIFIER_LENGTH)


@pytest.mark.parametrize(
    "name",
    [
        f"{TEMPLATE_PREFIX}{EPOCH}_{PID}",
        f"{TEMPLATE_PREFIX}{EPOCH}_{PID}_{WORKER}",
    ],
)
def test_the_epoch_is_read_from_a_template_name(name: str) -> None:
    assert template_epoch(name) == EPOCH


@pytest.mark.parametrize(
    "name",
    [
        "postgres",
        "bildo",
        f"{CLONE_PREFIX}{EPOCH}_{PID}_1",
        f"other_{TEMPLATE_PREFIX}{EPOCH}_{PID}",
        f"{TEMPLATE_PREFIX}backup",
        f"{TEMPLATE_PREFIX}{EPOCH}",
        f"{TEMPLATE_PREFIX}{EPOCH}_{PID}_a-b",
        f"{TEMPLATE_PREFIX}{EPOCH}_{PID}_a_b",
        f"{TEMPLATE_PREFIX}{EPOCH}_{PID};drop",
    ],
)
def test_a_name_that_is_not_a_template_name_has_no_epoch(name: str) -> None:
    assert template_epoch(name) is None


def test_only_templates_older_than_six_hours_are_stale() -> None:
    now = EPOCH + STALE_AFTER_SECONDS * 3
    old = template_name(now - STALE_AFTER_SECONDS - 1, PID, None)
    edge = template_name(now - STALE_AFTER_SECONDS, PID, None)
    young = template_name(now - 60, PID, WORKER)
    other = f"cyrano_other_{now - STALE_AFTER_SECONDS * 2}_{PID}"
    clone = f"{CLONE_PREFIX}{now - STALE_AFTER_SECONDS * 2}_{PID}_1"

    assert stale_templates(["postgres", old, edge, young, other, clone], now) == [old]


async def test_the_sweep_drops_only_old_templates_in_the_server() -> None:
    admin_url = read_admin_url()
    now = int(time.time())
    old = template_name(now - STALE_AFTER_SECONDS - 60, os.getpid(), "sweepold")
    young = template_name(now - 60, os.getpid(), "sweepyoung")
    other = f"cyrano_other_{now - STALE_AFTER_SECONDS - 60}_{os.getpid()}_sweepother"
    names = (old, young, other)
    try:
        for name in names:
            await create_database(admin_url, name)

        await drop_stale_templates(admin_url, now)

        remaining = await list_database_names(admin_url)
        assert old not in remaining
        assert young in remaining
        assert other in remaining
    finally:
        for name in names:
            await drop_database(admin_url, name)


async def test_waiting_for_a_database_with_an_open_connection_fails_and_without_one_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_url = read_admin_url()
    name = template_name(int(time.time()), os.getpid(), "waitbusy")
    monkeypatch.setattr(database_fixtures, "UNUSED_POLL_ATTEMPTS", 2)
    await create_database(admin_url, name)
    engine = create_async_engine(admin_url.set(database=name), poolclass=pool.NullPool)
    try:
        await wait_until_unused(admin_url, name)
        async with engine.connect():
            with pytest.raises(pytest.fail.Exception, match="open connections"):
                await wait_until_unused(admin_url, name)
    finally:
        await engine.dispose()
        await drop_database(admin_url, name)


async def test_the_template_is_built_without_connections_and_dropped_by_its_finalizer() -> None:
    request = RecordingRequest()
    now = int(time.time()) + LATER_TEMPLATE_OFFSET
    name = template_name(now, os.getpid(), os.environ.get(WORKER_ENV))
    try:
        template = await asyncio.to_thread(build_template, request, now)

        assert template.name == name
        assert name in await list_database_names(template.admin_url)
        assert await _connections(template.admin_url, name) == 0
        assert len(request.finalizers) == 1
    finally:
        await asyncio.to_thread(request.finish)

    assert name not in await list_database_names(read_admin_url())


async def test_the_template_is_dropped_even_when_the_migration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_migration(config: object) -> None:
        raise RuntimeError("invented migration failure")

    monkeypatch.setattr(database_fixtures, "migrate_to_head", failing_migration)
    request = RecordingRequest()
    now = int(time.time()) + LATER_TEMPLATE_OFFSET + 1
    name = template_name(now, os.getpid(), os.environ.get(WORKER_ENV))
    admin_url = read_admin_url()
    try:
        with pytest.raises(RuntimeError, match="invented migration failure"):
            await asyncio.to_thread(build_template, request, now)

        assert name in await list_database_names(admin_url)
    finally:
        await asyncio.to_thread(request.finish)

    assert name not in await list_database_names(admin_url)


async def test_the_template_of_this_run_is_in_the_server(
    template_database: TemplateDatabase,
) -> None:
    names = await list_database_names(template_database.admin_url)

    assert template_database.name in names
    assert template_database.name.startswith(TEMPLATE_PREFIX)
    assert template_epoch(template_database.name) is not None


async def test_the_database_of_a_test_is_a_clone_of_the_template(
    migrated_url: URL, template_database: TemplateDatabase
) -> None:
    assert migrated_url.database is not None
    assert CLONE_NAME_PATTERN.match(migrated_url.database)
    assert migrated_url.database != template_database.name
    assert os.environ["DATABASE_URL"] == migrated_url.render_as_string(hide_password=False)
    assert await fetch_all(migrated_url, "SELECT count(*) FROM alembic_version") == [(1,)]


async def test_two_clones_do_not_share_data(template_database: TemplateDatabase) -> None:
    async with (
        cloned_database(template_database) as first,
        cloned_database(template_database) as second,
    ):
        await execute(first, INSERT_CONTACT)

        assert await fetch_all(first, "SELECT count(*) FROM contacts") == [(1,)]
        assert await fetch_all(second, "SELECT count(*) FROM contacts") == [(0,)]
        assert first != second


async def test_a_clone_starts_empty_whatever_an_earlier_clone_wrote(
    template_database: TemplateDatabase,
) -> None:
    async with cloned_database(template_database) as first:
        await execute(first, INSERT_CONTACT)
        await execute(first, "UPDATE modes SET enabled = false")

    async with cloned_database(template_database) as second:
        assert await fetch_all(second, "SELECT count(*) FROM contacts") == [(0,)]
        assert await fetch_all(second, "SELECT DISTINCT enabled FROM modes") == [(True,)]


async def test_a_clone_is_dropped_when_its_block_ends(template_database: TemplateDatabase) -> None:
    async with cloned_database(template_database) as url:
        name = url.database
        assert name in await list_database_names(template_database.admin_url)

    assert name not in await list_database_names(template_database.admin_url)


async def test_a_clone_has_the_schema_and_the_seeds_of_a_freshly_migrated_database(
    template_database: TemplateDatabase, fresh_migrated_url: URL
) -> None:
    fresh = await _snapshot(fresh_migrated_url)

    async with cloned_database(template_database) as clone_url:
        clone = await _snapshot(clone_url)

    assert clone == fresh
    assert fresh["tables"]
    assert fresh["columns"]
    assert fresh["constraints"]
    assert fresh["indexes"]
    assert fresh["enums"]
    assert fresh["modes"]


async def test_a_clone_is_made_without_running_a_migration(
    template_database: TemplateDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_migration(config: object) -> None:
        raise AssertionError("a clone must not run a migration")

    monkeypatch.setattr(database_fixtures, "migrate_to_head", forbidden_migration)

    async with cloned_database(template_database) as url:
        revisions = await fetch_all(url, "SELECT version_num FROM alembic_version")
        tables = await fetch_all(url, SCHEMA_QUERIES["tables"])

    assert len(revisions) == 1
    assert tables


async def test_a_clone_carries_the_revision_of_the_template_and_of_a_fresh_database(
    template_database: TemplateDatabase, fresh_migrated_url: URL
) -> None:
    async with cloned_database(template_database) as url:
        clone_revisions = await fetch_all(url, "SELECT version_num FROM alembic_version")

    fresh_revisions = await fetch_all(fresh_migrated_url, "SELECT version_num FROM alembic_version")
    assert clone_revisions == fresh_revisions
