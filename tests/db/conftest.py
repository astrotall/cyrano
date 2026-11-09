from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import make_url, pool, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from cyrano.config import load_settings
from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
    SqlPersonaRepository,
    SqlScheduleProfileRepository,
    SqlSenderStateRepository,
    SqlStyleExampleRepository,
)
from cyrano.db.session import SessionFactory, build_engine, build_session_factory
from tests.conftest import OPTIONAL_ENV, REQUIRED_ENV
from tests.db.template import (
    UNUSED_POLL_ATTEMPTS,
    UNUSED_POLL_SECONDS,
    WORKER_ENV,
    clone_name,
    preserved_loggers,
    stale_templates,
    template_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
CONNECT_TIMEOUT_SECONDS = 5
HEAD_REVISION = "head"
MISSING_URL_HINT = (
    "TEST_DATABASE_URL is not set: database tests need a real PostgreSQL. "
    "Set it as described in the Testing section of README.md "
    "and start the server with `make db-up`."
)
UNREACHABLE_HINT = (
    "Cannot create a test database through TEST_DATABASE_URL ({error}). "
    "Is PostgreSQL running? Start it with `make db-up`."
)
TEMPLATE_BUSY_HINT = "The template database {name} still has open connections after migrating."


class FinalizerRegistry(Protocol):
    def addfinalizer(self, finalizer: Callable[[], object]) -> None: ...


@dataclass(frozen=True)
class TemplateDatabase:
    admin_url: URL
    name: str


class DatabaseTestSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    test_database_url: SecretStr


def read_admin_url(env_file: Path | None = ENV_FILE) -> URL:
    try:
        settings = DatabaseTestSettings(_env_file=env_file)
    except ValidationError:
        pytest.fail(MISSING_URL_HINT, pytrace=False)
    return make_url(settings.test_database_url.get_secret_value())


async def _execute_as_admin(admin_url: URL, statement: str) -> None:
    engine = create_async_engine(
        admin_url,
        poolclass=pool.NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"timeout": CONNECT_TIMEOUT_SECONDS},
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def create_database(admin_url: URL, name: str, template: str | None = None) -> None:
    statement = f'CREATE DATABASE "{name}"'
    if template is not None:
        statement += f' TEMPLATE "{template}"'
    try:
        await _execute_as_admin(admin_url, statement)
    except Exception as exc:
        error = type(exc).__name__
    else:
        return
    pytest.fail(UNREACHABLE_HINT.format(error=error), pytrace=False)


async def drop_database(admin_url: URL, name: str) -> None:
    await _execute_as_admin(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def list_database_names(admin_url: URL) -> list[str]:
    rows = await fetch_all(admin_url, "SELECT datname FROM pg_database")
    return [str(row[0]) for row in rows]


async def drop_stale_templates(admin_url: URL, now: int) -> None:
    for name in stale_templates(await list_database_names(admin_url), now):
        await drop_database(admin_url, name)


async def wait_until_unused(admin_url: URL, name: str) -> None:
    for _ in range(UNUSED_POLL_ATTEMPTS):
        [(connections,)] = await fetch_all(
            admin_url,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = :name",
            {"name": name},
        )
        if connections == 0:
            return
        await asyncio.sleep(UNUSED_POLL_SECONDS)
    pytest.fail(TEMPLATE_BUSY_HINT.format(name=name), pytrace=False)


def migrate_to_head(config: Config) -> None:
    command.upgrade(config, HEAD_REVISION)


async def fetch_all(
    url: URL, statement: str, params: dict[str, str] | None = None
) -> list[tuple[object, ...]]:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params or {})
            return [tuple(row) for row in result.all()]
    finally:
        await engine.dispose()


async def execute(url: URL, statement: str) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture
def alembic_config() -> Config:
    return Config(ALEMBIC_INI)


@pytest.fixture
async def database_url(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[URL]:
    admin_url = read_admin_url()
    name = f"cyrano_test_{uuid4().hex}"
    await create_database(admin_url, name)
    url = admin_url.set(database=name)
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    try:
        yield url
    finally:
        await drop_database(admin_url, name)


def build_template(request: FinalizerRegistry, now: int) -> TemplateDatabase:
    admin_url = read_admin_url()
    asyncio.run(drop_stale_templates(admin_url, now))
    name = template_name(now, os.getpid(), os.environ.get(WORKER_ENV))
    asyncio.run(create_database(admin_url, name))
    request.addfinalizer(lambda: asyncio.run(drop_database(admin_url, name)))
    url = admin_url.set(database=name)
    with pytest.MonkeyPatch.context() as patch, preserved_loggers():
        for variable in OPTIONAL_ENV:
            patch.delenv(variable, raising=False)
        for variable, value in REQUIRED_ENV.items():
            patch.setenv(variable, value)
        patch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
        migrate_to_head(Config(ALEMBIC_INI))
    asyncio.run(wait_until_unused(admin_url, name))
    return TemplateDatabase(admin_url=admin_url, name=name)


@pytest.fixture(scope="session")
def template_database(request: pytest.FixtureRequest) -> TemplateDatabase:
    return build_template(request, int(time.time()))


@asynccontextmanager
async def cloned_database(template: TemplateDatabase) -> AsyncIterator[URL]:
    name = clone_name(int(time.time()), os.getpid(), os.environ.get(WORKER_ENV))
    await create_database(template.admin_url, name, template=template.name)
    try:
        yield template.admin_url.set(database=name)
    finally:
        await drop_database(template.admin_url, name)


@pytest.fixture
async def migrated_url(
    template_database: TemplateDatabase, full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[URL]:
    async with cloned_database(template_database) as url:
        monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
        yield url


@pytest.fixture
async def fresh_migrated_url(database_url: URL, alembic_config: Config) -> URL:
    await asyncio.to_thread(migrate_to_head, alembic_config)
    return database_url


@pytest.fixture
async def session_factory(migrated_url: URL) -> AsyncIterator[SessionFactory]:
    engine = build_engine(load_settings(env_file=None))
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def contacts(session_factory: SessionFactory) -> SqlContactRepository:
    return SqlContactRepository(session_factory)


@pytest.fixture
def messages(session_factory: SessionFactory) -> SqlMessageRepository:
    return SqlMessageRepository(session_factory)


@pytest.fixture
def modes(session_factory: SessionFactory) -> SqlModeRepository:
    return SqlModeRepository(session_factory)


@pytest.fixture
def outbox(session_factory: SessionFactory) -> SqlOutboxRepository:
    return SqlOutboxRepository(session_factory)


@pytest.fixture
def sender_state(session_factory: SessionFactory) -> SqlSenderStateRepository:
    return SqlSenderStateRepository(session_factory)


@pytest.fixture
def schedule_profiles(session_factory: SessionFactory) -> SqlScheduleProfileRepository:
    return SqlScheduleProfileRepository(session_factory)


@pytest.fixture
def personas(session_factory: SessionFactory) -> SqlPersonaRepository:
    return SqlPersonaRepository(session_factory)


@pytest.fixture
def style_examples(session_factory: SessionFactory) -> SqlStyleExampleRepository:
    return SqlStyleExampleRepository(session_factory)
