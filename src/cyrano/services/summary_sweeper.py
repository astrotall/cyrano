from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from cyrano.domain.autopilot import SWEEP_INTERVAL_SECONDS
from cyrano.domain.summary import SUMMARY_FAILURE_COOLDOWN_SECONDS, SUMMARY_SWEEP_LIMIT
from cyrano.services.repositories import SummaryRepository
from cyrano.services.summary_job_queue import SummaryJobQueue

logger = logging.getLogger("cyrano.services.summary_sweeper")


class SummarySweeper:
    def __init__(
        self,
        summaries: SummaryRepository,
        queue: SummaryJobQueue,
        *,
        enabled: bool,
        every_messages: int,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = SWEEP_INTERVAL_SECONDS,
        limit: int = SUMMARY_SWEEP_LIMIT,
    ) -> None:
        self._summaries = summaries
        self._queue = queue
        self._enabled = enabled
        self._every_messages = every_messages
        self._clock = clock
        self._sleep = sleep
        self._interval = interval
        self._limit = limit

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception as exc:
                logger.error("summary sweep failed", extra={"error": type(exc).__name__})
            await self._sleep(self._interval)

    async def sweep(self) -> int:
        if not self._enabled:
            return 0
        due = await self._summaries.contacts_due_for_summary(
            every=self._every_messages,
            cooldown=timedelta(seconds=SUMMARY_FAILURE_COOLDOWN_SECONDS),
            now=self._clock(),
            limit=self._limit,
        )
        enqueued = 0
        failure: str | None = None
        for item in due:
            try:
                await self._queue.enqueue(item.contact_id, item.up_to_message_id)
            except Exception as exc:
                failure = type(exc).__name__
                continue
            enqueued += 1
        if failure is not None:
            logger.warning(
                "summary sweep could not enqueue every job",
                extra={"found": len(due), "enqueued": enqueued, "error": failure},
            )
        elif due:
            logger.info("summary sweep", extra={"found": len(due), "enqueued": enqueued})
        return enqueued
