from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from cyrano.domain.early_read import EARLY_READ_MIN_INTERVAL_SECONDS, EarlyReadRequest
from cyrano.domain.enums import ContactStatus, EarlyReadReason
from cyrano.domain.models import Contact
from cyrano.services.humanization import HumanizationPlanner, ScheduleService
from cyrano.services.repositories import (
    AutopilotStateRepository,
    ContactRepository,
    SenderStateRepository,
)

logger = logging.getLogger("cyrano.services.early_read")


class EarlyReadSettings(Protocol):
    @property
    def autopilot_enabled(self) -> bool: ...

    @property
    def early_read_enabled(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class EarlyReadVerdict:
    reason: EarlyReadReason | None
    contact: Contact | None = None

    @property
    def allowed(self) -> bool:
        return self.reason is None


class EarlyReadQueue:
    def __init__(self, min_interval_seconds: float = EARLY_READ_MIN_INTERVAL_SECONDS) -> None:
        self._interval = timedelta(seconds=min_interval_seconds)
        self._pending: dict[int, EarlyReadRequest] = {}
        self._last_executed: dict[int, datetime] = {}

    def register(self, contact_id: int, due_at: datetime) -> bool:
        if contact_id in self._pending:
            return False
        self._pending[contact_id] = EarlyReadRequest(
            contact_id, self._not_before(contact_id, due_at)
        )
        return True

    def take_due(self, now: datetime) -> list[EarlyReadRequest]:
        due: list[EarlyReadRequest] = []
        for contact_id, request in list(self._pending.items()):
            if request.due_at > now:
                continue
            allowed_at = self._not_before(contact_id, request.due_at)
            if allowed_at > now:
                self._pending[contact_id] = replace(request, due_at=allowed_at)
                continue
            del self._pending[contact_id]
            due.append(request)
        self._forget_old(now)
        return sorted(due, key=lambda request: (request.due_at, request.contact_id))

    def record_executed(self, contact_id: int, at: datetime) -> None:
        self._last_executed[contact_id] = at

    def pending(self) -> list[EarlyReadRequest]:
        return sorted(self._pending.values(), key=lambda request: request.contact_id)

    def _not_before(self, contact_id: int, due_at: datetime) -> datetime:
        last = self._last_executed.get(contact_id)
        return due_at if last is None else max(due_at, last + self._interval)

    def _forget_old(self, now: datetime) -> None:
        self._last_executed = {
            contact_id: last
            for contact_id, last in self._last_executed.items()
            if last + self._interval > now
        }


class EarlyReadGate:
    def __init__(
        self,
        contacts: ContactRepository,
        autopilot_state: AutopilotStateRepository,
        sender_state: SenderStateRepository,
        schedule: ScheduleService,
        switch: EarlyReadSettings,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._contacts = contacts
        self._autopilot_state = autopilot_state
        self._sender_state = sender_state
        self._schedule = schedule
        self._switch = switch
        self._clock = clock

    async def check(self, contact_id: int) -> EarlyReadVerdict:
        now = self._clock()
        if not self._switch.early_read_enabled:
            return EarlyReadVerdict(EarlyReadReason.DISABLED)
        if not self._switch.autopilot_enabled:
            return EarlyReadVerdict(EarlyReadReason.AUTOPILOT_OFF)
        autopilot = await self._autopilot_state.get()
        if autopilot.stopped:
            return EarlyReadVerdict(EarlyReadReason.AUTOPILOT_STOPPED)
        if autopilot.is_paused(now):
            return EarlyReadVerdict(EarlyReadReason.AUTOPILOT_PAUSED)
        sender = await self._sender_state.get()
        if sender.stopped:
            return EarlyReadVerdict(EarlyReadReason.SENDER_STOPPED)
        if sender.is_paused(now):
            return EarlyReadVerdict(EarlyReadReason.SENDER_PAUSED)
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return EarlyReadVerdict(EarlyReadReason.CONTACT_MISSING)
        if contact.status is not ContactStatus.ACTIVE:
            return EarlyReadVerdict(EarlyReadReason.CONTACT_NOT_ACTIVE)
        if await self._schedule.blocked_until(now) is not None:
            return EarlyReadVerdict(EarlyReadReason.BLOCKED_WINDOW)
        return EarlyReadVerdict(None, contact)


class EarlyReadScheduler:
    def __init__(
        self,
        gate: EarlyReadGate,
        planner: HumanizationPlanner,
        schedule: ScheduleService,
        queue: EarlyReadQueue,
        *,
        rng: random.Random,
        clock: Callable[[], datetime],
    ) -> None:
        self._gate = gate
        self._planner = planner
        self._schedule = schedule
        self._queue = queue
        self._rng = rng
        self._clock = clock

    async def on_incoming(self, contact_id: int, message_id: int) -> bool:
        try:
            return await self._register(contact_id, message_id)
        except Exception as exc:
            logger.warning(
                "early read was not requested",
                extra={"contact_id": contact_id, "error": type(exc).__name__},
            )
            return False

    async def _register(self, contact_id: int, message_id: int) -> bool:
        verdict = await self._gate.check(contact_id)
        if not verdict.allowed:
            return False
        if not await self._planner.conversation_ongoing(contact_id, message_id):
            return False
        profile = await self._schedule.get_profile()
        delay = self._rng.uniform(*profile.early_read_seconds)
        registered = self._queue.register(contact_id, self._clock() + timedelta(seconds=delay))
        if registered:
            logger.info(
                "early read requested",
                extra={"contact_id": contact_id, "trigger_message_id": message_id},
            )
        return registered
