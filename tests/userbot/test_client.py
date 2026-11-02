from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from cyrano.config import Settings, load_settings
from cyrano.userbot import client as client_module
from cyrano.userbot.client import (
    _telegram_client,
    connect_authorized,
    disconnect_client,
    ensure_session_exists,
    secure_session_file,
    session_file,
)
from cyrano.userbot.errors import (
    ConnectFailed,
    ConnectTimedOut,
    SessionMissing,
    SessionNotAuthorized,
)
from tests.userbot.fakes import FakeClient, touch_session_file


def _settings(session_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("USERBOT_SESSION_PATH", str(session_path))
    return load_settings(env_file=None)


def test_session_file_follows_telethons_naming() -> None:
    assert session_file(Path("data/session/userbot")) == Path("data/session/userbot.session")
    assert session_file(Path("data/session/userbot.session")) == Path(
        "data/session/userbot.session"
    )


def test_missing_session_file_is_a_readable_error(tmp_path: Path) -> None:
    with pytest.raises(SessionMissing) as raised:
        ensure_session_exists(tmp_path / "userbot")

    assert "make login" in str(raised.value)


def test_existing_session_file_passes(tmp_path: Path) -> None:
    touch_session_file(tmp_path / "userbot.session")

    ensure_session_exists(tmp_path / "userbot")


def test_a_directory_with_the_session_name_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "userbot.session").mkdir()

    with pytest.raises(SessionMissing):
        ensure_session_exists(tmp_path / "userbot")


def test_building_the_client_creates_the_session_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_env: dict[str, str]
) -> None:
    path = tmp_path / "does" / "not" / "exist" / "userbot"
    settings = _settings(path, monkeypatch)

    telegram_client = _telegram_client(settings)
    try:
        assert path.parent.is_dir()
        assert telegram_client.is_connected() in (False, None)
        assert telegram_client.api_id == int(full_env["TELEGRAM_API_ID"])
    finally:
        telegram_client.session.close()


def test_secure_session_file_sets_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "userbot.session"
    touch_session_file(path)
    path.chmod(0o644)

    secure_session_file(tmp_path / "userbot")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secure_session_file_ignores_a_missing_file(tmp_path: Path) -> None:
    secure_session_file(tmp_path / "userbot")


async def test_authorized_session_connects_and_stays_connected() -> None:
    client = FakeClient()

    await connect_authorized(client)

    assert client.calls == ["connect", "is_user_authorized"]


async def test_unauthorized_session_is_a_readable_error_and_disconnects() -> None:
    client = FakeClient(authorized=False)

    with pytest.raises(SessionNotAuthorized) as raised:
        await connect_authorized(client)

    assert "make login" in str(raised.value)
    assert client.calls[-1] == "disconnect"


async def test_connect_timeout_is_a_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "CONNECT_TIMEOUT_SECONDS", 0.05)
    client = FakeClient(hang_on_connect=True)

    with pytest.raises(ConnectTimedOut) as raised:
        await asyncio.wait_for(connect_authorized(client), timeout=2)

    assert "within 0.05 seconds" in str(raised.value)
    assert client.calls == ["connect", "disconnect"]


async def test_connection_refused_is_a_readable_error() -> None:
    client = FakeClient(connect_error=ConnectionRefusedError("fake detail that stays out"))

    with pytest.raises(ConnectFailed) as raised:
        await connect_authorized(client)

    assert "ConnectionRefusedError" in str(raised.value)
    assert "fake detail" not in str(raised.value)
    assert client.calls[-1] == "disconnect"


async def test_disconnect_failures_and_hangs_are_contained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "DISCONNECT_TIMEOUT_SECONDS", 0.05)

    class _Hanging(FakeClient):
        async def _disconnect(self) -> None:
            await asyncio.sleep(3600)

    class _Broken(FakeClient):
        async def _disconnect(self) -> None:
            raise RuntimeError

    await asyncio.wait_for(disconnect_client(_Hanging()), timeout=2)
    await disconnect_client(_Broken())
