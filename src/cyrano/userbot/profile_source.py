"""Read-only Telethon calls that fetch a contact's profile and photo."""

from __future__ import annotations

from pathlib import Path

from telethon.errors import RPCError
from telethon.tl.functions.users import GetFullUserRequest

from cyrano.domain.models import TelegramProfile
from cyrano.services.profile_source import ProfileSourceError
from cyrano.userbot.handler import display_name_of
from cyrano.userbot.protocols import UserbotClient

TELETHON_FAILURES = (RPCError, ValueError, OSError, TimeoutError)


class TelethonProfileSource:
    def __init__(self, client: UserbotClient) -> None:
        self._client = client

    async def fetch_profile(self, tg_id: int) -> TelegramProfile:
        try:
            user = await self._client.get_entity(tg_id)
            full = await self._client(GetFullUserRequest(user))
        except TELETHON_FAILURES as exc:
            raise ProfileSourceError(type(exc).__name__) from None
        return TelegramProfile(
            username=user.username or None,
            display_name=display_name_of(user),
            bio=full.full_user.about or None,
        )

    async def download_photo(self, tg_id: int, destination: Path) -> bool:
        try:
            user = await self._client.get_entity(tg_id)
            saved = await self._client.download_profile_photo(
                user, file=str(destination), download_big=True
            )
        except TELETHON_FAILURES as exc:
            raise ProfileSourceError(type(exc).__name__) from None
        return bool(saved)
