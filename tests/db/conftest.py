from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
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
    SqlStyleExampleRepository,
)
from cyrano.db.session import SessionFactory, build_engine, build_session_factory

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
CONNECT_TIMEOUT_SECONDS = 5
MISSING_URL_HINT = (
    "TEST_DATABASE_URL is not set: database tests need a real PostgreSQL. "
    "Set it as described in the Testing section of README.md "
    "and start the server with `make db-up`."
)
UNREACHABLE_HINT = (
    "Cannot create a test database through TEST_DATABASE_URL ({error}). "
    "Is PostgreSQL running? Start it with `make db-up`."
)


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


async def create_database(admin_url: URL, name: str) -> None:
    try:
        await _execute_as_admin(admin_url, f'CREATE DATABASE "{name}"')
    except Exception as exc:
        error = type(exc).__name__
    else:
        return
    pytest.fail(UNREACHABLE_HINT.format(error=error), pytrace=False)


async def drop_database(admin_url: URL, name: str) -> None:
    await _execute_as_admin(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


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


@pytest.fixture
async def migrated_url(database_url: URL, alembic_config: Config) -> URL:
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
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
def personas(session_factory: SessionFactory) -> SqlPersonaRepository:
    return SqlPersonaRepository(session_factory)


@pytest.fixture
def style_examples(session_factory: SessionFactory) -> SqlStyleExampleRepository:
    return SqlStyleExampleRepository(session_factory)
