from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from cyrano.config import load_settings
from cyrano.db.repositories import (
    SqlContactRepository,
    SqlMessageRepository,
    SqlModeRepository,
    SqlOutboxRepository,
)
from cyrano.db.session import SessionFactory, build_engine, build_session_factory
from cyrano.services.repositories import (
    ContactRepository,
    MessageRepository,
    ModeRepository,
    OutboxRepository,
)
from tests.conftest import SECRET_VALUES


async def test_engine_comes_from_settings_and_hides_the_password(
    full_env: dict[str, str],
) -> None:
    engine = build_engine(load_settings(env_file=None))
    try:
        assert engine.url.host == "postgres"
        assert engine.sync_engine.hide_parameters is True
        for secret in SECRET_VALUES:
            assert secret not in repr(engine)
    finally:
        await engine.dispose()


async def test_session_factory_keeps_objects_usable_after_commit(
    full_env: dict[str, str],
) -> None:
    engine = build_engine(load_settings(env_file=None))
    try:
        factory = build_session_factory(engine)
        assert factory.kw["expire_on_commit"] is False
    finally:
        await engine.dispose()


def test_repositories_satisfy_the_service_protocols() -> None:
    factory: SessionFactory = async_sessionmaker()

    contacts: ContactRepository = SqlContactRepository(factory)
    messages: MessageRepository = SqlMessageRepository(factory)
    modes: ModeRepository = SqlModeRepository(factory)
    outbox: OutboxRepository = SqlOutboxRepository(factory)

    assert outbox is not None
    assert contacts is not None
    assert messages is not None
    assert modes is not None
