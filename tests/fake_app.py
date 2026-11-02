from __future__ import annotations

import asyncio
import sys

from cyrano.config import load_settings
from cyrano.logging import configure_logging
from cyrano.main import run
from cyrano.userbot.protocols import UserbotClient
from tests.userbot.fakes import FakeClient

HANGING_CONNECT_SCENARIO = "hanging-connect"
CONNECTING_MARKER = "fake-client-connecting"


class _AnnouncingHangingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__(hang_on_connect=True)

    async def connect(self) -> None:
        print(CONNECTING_MARKER, flush=True)
        await super().connect()


def _fake_client(_: object) -> UserbotClient:
    return FakeClient()


def _hanging_client(_: object) -> UserbotClient:
    return _AnnouncingHangingClient()


def main(argv: list[str]) -> int:
    settings = load_settings(env_file=None)
    configure_logging(settings.log_level, settings.library_log_levels)
    factory = _hanging_client if HANGING_CONNECT_SCENARIO in argv else _fake_client
    asyncio.run(run(settings, client_factory=factory))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
