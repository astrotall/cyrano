from __future__ import annotations

import socket
from pathlib import Path

import pytest
from sqlalchemy import make_url

from tests.db.conftest import create_database, read_admin_url


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


async def test_unreachable_postgres_fails_and_names_make_db_up() -> None:
    url = make_url(f"postgresql+asyncpg://fake:db-password-secret@127.0.0.1:{_closed_port()}/fake")

    with pytest.raises(pytest.fail.Exception, match="make db-up") as caught:
        await create_database(url, "cyrano_test_never_created")

    assert "db-password-secret" not in str(caught.value)


def test_missing_url_fails_and_names_make_db_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    with pytest.raises(pytest.fail.Exception, match="make db-up"):
        read_admin_url(tmp_path / "missing.env")
