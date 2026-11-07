"""Puts reply jobs on the queue: once per incoming message, and by a sweep for lost ones."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from cyrano.domain.autopilot import (
    REPLY_STALE_AFTER_SECONDS,
    SWEEP_DEBOUNCE_FACTOR,
    SWEEP_INTERVAL_SECONDS,
)
from cyrano.domain.enums import ContactStatus
from cyrano.services.reply_job_queue import ReplyJobQueue
from cyrano.services.repositories import (
    AutopilotStateRepository,
    ContactRepository,
    ReplyBacklogRepository,
)
from cyrano.services.send_guard import GuardSettings

logger = logging.getLogger("cyrano.services.autopilot")

SWEEP_DEFER_SECONDS = 0


async def autopilot_running(
    switch: GuardSettings, state: AutopilotStateRepository, now: datetime
) -> bool:
    if not switch.autopilot_enabled:
        return False
    return (await state.get()).is_running(now)


class AutopilotScheduler:
    def __init__(
        self,
        contacts: ContactRepository,
        state: AutopilotStateRepository,
        queue: ReplyJobQueue,
        switch: GuardSettings,
        *,
        debounce_seconds: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._contacts = contacts
        self._state = state
        self._queue = queue
        self._switch = switch
        self._debounce_seconds = debounce_seconds
        self._clock = clock

    async def on_incoming(self, contact_id: int, message_id: int) -> bool:
        try:
            return await self._schedule(contact_id, message_id)
        except Exception as exc:
            logger.warning(
                "reply job was not enqueued",
                extra={"contact_id": contact_id, "error": type(exc).__name__},
            )
            return False

    async def _schedule(self, contact_id: int, message_id: int) -> bool:
        if not await autopilot_running(self._switch, self._state, self._clock()):
            return False
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None or contact.status is not ContactStatus.ACTIVE:
            return False
        await self._queue.enqueue(contact_id, message_id, self._debounce_seconds)
        logger.info(
            "reply job enqueued",
            extra={
                "contact_id": contact_id,
                "trigger_message_id": message_id,
                "defer_seconds": self._debounce_seconds,
            },
        )
        return True


class AutopilotSweeper:
    def __init__(
        self,
        backlog: ReplyBacklogRepository,
        state: AutopilotStateRepository,
        queue: ReplyJobQueue,
        switch: GuardSettings,
        *,
        debounce_seconds: int,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self._backlog = backlog
        self._state = state
        self._queue = queue
        self._switch = switch
        self._debounce_seconds = debounce_seconds
        self._clock = clock
        self._sleep = sleep
        self._interval = interval

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception as exc:
                logger.error("autopilot sweep failed", extra={"error": type(exc).__name__})
            await self._sleep(self._interval)

    async def sweep(self) -> int:
        now = self._clock()
        if not await autopilot_running(self._switch, self._state, now):
            return 0
        waiting = await self._backlog.awaiting_reply(
            newer_than=now - timedelta(seconds=REPLY_STALE_AFTER_SECONDS),
            older_than=now - timedelta(seconds=SWEEP_DEBOUNCE_FACTOR * self._debounce_seconds),
        )
        enqueued = 0
        failure: str | None = None
        for message in waiting:
            try:
                await self._queue.enqueue(message.contact_id, message.id, SWEEP_DEFER_SECONDS)
            except Exception as exc:
                failure = type(exc).__name__
                continue
            enqueued += 1
        if failure is not None:
            logger.warning(
                "autopilot sweep could not enqueue every job",
                extra={"found": len(waiting), "enqueued": enqueued, "error": failure},
            )
        elif waiting:
            logger.info("autopilot sweep", extra={"found": len(waiting), "enqueued": enqueued})
        return enqueued
