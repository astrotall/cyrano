from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from cyrano.domain.autopilot import AutopilotState
from cyrano.domain.enums import AutopilotPauseReason, AutopilotStopReason
from cyrano.domain.models import Message
from tests.services.outbox_fakes import FAKE_NOW


class InMemoryAutopilotState:
    def __init__(self) -> None:
        self.state = AutopilotState(
            stopped=False,
            stop_reason=None,
            paused_until=None,
            pause_reason=None,
            updated_at=FAKE_NOW,
        )
        self.stops: list[AutopilotStopReason] = []
        self.pauses: list[tuple[datetime, AutopilotPauseReason]] = []

    async def get(self) -> AutopilotState:
        return self.state

    async def stop(self, reason: AutopilotStopReason) -> bool:
        self.stops.append(reason)
        if self.state.stopped:
            return False
        self.state = self.state.model_copy(update={"stopped": True, "stop_reason": reason})
        return True

    async def pause_until(
        self, until: datetime, reason: AutopilotPauseReason, *, now: datetime
    ) -> bool:
        self.pauses.append((until, reason))
        current = self.state.paused_until
        if current is None or until > current:
            self.state = self.state.model_copy(
                update={"paused_until": until, "pause_reason": reason}
            )
        return current is None or current <= now

    async def resume(self) -> AutopilotState:
        before = self.state
        self.state = before.model_copy(
            update={
                "stopped": False,
                "stop_reason": None,
                "paused_until": None,
                "pause_reason": None,
            }
        )
        return before


class InMemoryReplyBacklog:
    def __init__(self, waiting: list[Message] | None = None, unanswered: int = 0) -> None:
        self.waiting = waiting or []
        self.unanswered = unanswered
        self.queries: list[tuple[datetime, datetime]] = []

    async def awaiting_reply(self, *, newer_than: datetime, older_than: datetime) -> list[Message]:
        self.queries.append((newer_than, older_than))
        return list(self.waiting)

    async def count_unanswered_active(self) -> int:
        return self.unanswered


@dataclass
class RecordingQueue:
    error: Exception | None = None
    jobs: list[tuple[int, int, int]] = field(default_factory=list)
    started: int = 0
    closed: int = 0

    async def start(self) -> None:
        self.started += 1

    async def enqueue(self, contact_id: int, trigger_message_id: int, defer_seconds: int) -> None:
        if self.error is not None:
            raise self.error
        self.jobs.append((contact_id, trigger_message_id, defer_seconds))

    async def close(self) -> None:
        self.closed += 1
