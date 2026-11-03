from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from telethon.errors import FloodWaitError, UserPrivacyRestrictedError

from cyrano.services.profile_source import ProfileSource, ProfileSourceError
from cyrano.userbot.profile_source import TelethonProfileSource
from tests.userbot.fakes import FakeClient, FakeSender

TG_ID = 5001
READ_METHODS = {"get_entity", "get_full_user", "download_profile_photo"}


def _client(
    *, about: str | None = "fake bio", user: FakeSender | None = None, **kwargs: object
) -> FakeClient:
    client = FakeClient(**kwargs)  # type: ignore[arg-type]
    client.users[TG_ID] = user if user is not None else FakeSender(id=TG_ID)
    client.about = about
    return client


def test_the_adapter_satisfies_the_service_interface() -> None:
    source: ProfileSource = TelethonProfileSource(FakeClient())

    assert source is not None


async def test_the_username_name_and_bio_are_fetched() -> None:
    client = _client(user=FakeSender(id=TG_ID, username="fake_user", first_name="A", last_name="B"))

    profile = await TelethonProfileSource(client).fetch_profile(TG_ID)

    assert (profile.username, profile.display_name, profile.bio) == ("fake_user", "A B", "fake bio")
    assert client.calls == ["get_entity", "get_full_user"]
    assert client.full_user_requests == [TG_ID]


async def test_missing_username_names_and_bio_become_none() -> None:
    user = FakeSender(id=TG_ID, username=None, first_name=None, last_name=None)
    client = _client(about="", user=user)

    profile = await TelethonProfileSource(client).fetch_profile(TG_ID)

    assert (profile.username, profile.display_name, profile.bio) == (None, None, None)


async def test_a_user_that_is_not_in_the_session_cache_is_a_source_error() -> None:
    client = FakeClient()

    with pytest.raises(ProfileSourceError, match="ValueError"):
        await TelethonProfileSource(client).fetch_profile(TG_ID)


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (FloodWaitError(request=None, capture=3600), "FloodWaitError"),
        (UserPrivacyRestrictedError(request=None), "UserPrivacyRestrictedError"),
        (TimeoutError(), "TimeoutError"),
        (ConnectionError(), "ConnectionError"),
        (OSError(), "OSError"),
    ],
)
async def test_every_adapter_failure_is_one_source_error_carrying_only_the_class_name(
    error: Exception, name: str
) -> None:
    client = _client(read_error=error)
    source = TelethonProfileSource(client)

    with pytest.raises(ProfileSourceError) as fetch_failure:
        await source.fetch_profile(TG_ID)
    with pytest.raises(ProfileSourceError) as photo_failure:
        await source.download_photo(TG_ID, Path("unused.jpg"))

    assert str(fetch_failure.value) == name
    assert str(photo_failure.value) == name


async def test_a_flood_wait_fails_at_once_instead_of_sleeping() -> None:
    client = _client(read_error=FloodWaitError(request=None, capture=3600))

    async with asyncio.timeout(1):
        with pytest.raises(ProfileSourceError):
            await TelethonProfileSource(client).fetch_profile(TG_ID)


async def test_the_big_current_photo_is_downloaded_to_the_destination(tmp_path: Path) -> None:
    client = _client()
    client.photo_bytes = b"fake-jpeg"
    destination = tmp_path / "1.jpg"

    saved = await TelethonProfileSource(client).download_photo(TG_ID, destination)

    assert saved is True
    assert destination.read_bytes() == b"fake-jpeg"
    assert client.photo_downloads == [(str(destination), True)]


async def test_a_user_without_a_photo_saves_nothing(tmp_path: Path) -> None:
    client = _client()
    destination = tmp_path / "1.jpg"

    saved = await TelethonProfileSource(client).download_photo(TG_ID, destination)

    assert saved is False
    assert not destination.exists()


async def test_only_read_methods_are_used(tmp_path: Path) -> None:
    client = _client()
    client.photo_bytes = b"x"
    source = TelethonProfileSource(client)

    await source.fetch_profile(TG_ID)
    await source.download_photo(TG_ID, tmp_path / "1.jpg")

    assert client.write_attempts == []
    assert set(client.calls) <= READ_METHODS
