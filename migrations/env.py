"""Alembic environment: async engine, URL from the application settings."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from cyrano.config import ConfigError, load_settings
from cyrano.db.models import Base
from cyrano.logging import configure_logging


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online(url: str) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


def _run_offline(url: str) -> None:
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    url = settings.database_url.get_secret_value()
    if context.is_offline_mode():
        _run_offline(url)
        return
    configure_logging(settings.log_level, settings.library_log_levels)
    asyncio.run(_run_online(url))


_main()
