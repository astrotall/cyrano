from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

from cyrano import main as main_module
from cyrano.config import load_settings
from cyrano.logging import configure_logging
from cyrano.main import run
from cyrano.userbot.client import UserbotAccess
from tests.bot.fakes import fake_control_bot_class
from tests.services.autopilot_fakes import (
    InMemoryAutopilotState,
    InMemoryReplyBacklog,
    RecordingQueue,
)
from tests.services.fakes import InMemoryModes
from tests.services.outbox_fakes import (
    ALWAYS_FREE_PROFILE,
    InMemoryOutbox,
    InMemoryScheduleProfiles,
    InMemorySenderState,
    ScriptedTransport,
)
from tests.userbot.fakes import FakeClient, InMemoryContacts, InMemoryMessages

HANGING_CONNECT_SCENARIO = "hanging-connect"
CONNECTING_MARKER = "fake-client-connecting"


class _AnnouncingHangingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__(hang_on_connect=True)

    async def connect(self) -> None:
        print(CONNECTING_MARKER, flush=True)
        await super().connect()


def _fake_client(_: object) -> UserbotAccess:
    return UserbotAccess(client=FakeClient(), transport=ScriptedTransport())


def _hanging_client(_: object) -> UserbotAccess:
    return UserbotAccess(client=_AnnouncingHangingClient(), transport=ScriptedTransport())


def _in_memory_contacts(_: object) -> InMemoryContacts:
    return InMemoryContacts()


def _in_memory_messages(_: object) -> InMemoryMessages:
    return InMemoryMessages()


def _in_memory_modes(_: object) -> InMemoryModes:
    return InMemoryModes()


def _in_memory_outbox(_: object) -> InMemoryOutbox:
    return InMemoryOutbox()


def _in_memory_sender_state(_: object) -> InMemorySenderState:
    return InMemorySenderState()


def _in_memory_schedule_profiles(_: object) -> InMemoryScheduleProfiles:
    return InMemoryScheduleProfiles(ALWAYS_FREE_PROFILE)


def _in_memory_autopilot_state(_: object) -> InMemoryAutopilotState:
    return InMemoryAutopilotState()


def _in_memory_backlog(_: object) -> InMemoryReplyBacklog:
    return InMemoryReplyBacklog()


def _recording_queue(_: object) -> RecordingQueue:
    return RecordingQueue()


def main(argv: list[str]) -> int:
    settings = load_settings(env_file=None)
    configure_logging(settings.log_level, settings.library_log_levels)
    factory = _hanging_client if HANGING_CONNECT_SCENARIO in argv else _fake_client
    with (
        patch.object(main_module, "SqlContactRepository", _in_memory_contacts),
        patch.object(main_module, "SqlMessageRepository", _in_memory_messages),
        patch.object(main_module, "SqlModeRepository", _in_memory_modes),
        patch.object(main_module, "SqlOutboxRepository", _in_memory_outbox),
        patch.object(main_module, "SqlSenderStateRepository", _in_memory_sender_state),
        patch.object(main_module, "SqlScheduleProfileRepository", _in_memory_schedule_profiles),
        patch.object(main_module, "SqlAutopilotStateRepository", _in_memory_autopilot_state),
        patch.object(main_module, "SqlReplyBacklogRepository", _in_memory_backlog),
        patch.object(main_module, "ArqReplyJobQueue", _recording_queue),
        patch.object(main_module, "ControlBot", fake_control_bot_class()),
    ):
        asyncio.run(run(settings, client_factory=factory))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
