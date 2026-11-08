from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from cyrano.domain.autopilot import SWEEP_INTERVAL_SECONDS
from cyrano.domain.proactive import PROACTIVE_SWEEP_LIMIT
from cyrano.services.proactive_eligibility import ProactiveEligibility, hours, switch_reason
from cyrano.services.proactive_job_queue import ProactiveJobQueue
from cyrano.services.repositories import ProactiveRepository

logger = logging.getLogger("cyrano.services.proactive_sweeper")


class ProactiveSweeper:
    def __init__(
        self,
        proactive: ProactiveRepository,
        eligibility: ProactiveEligibility,
        queue: ProactiveJobQueue,
        *,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = SWEEP_INTERVAL_SECONDS,
        limit: int = PROACTIVE_SWEEP_LIMIT,
    ) -> None:
        self._proactive = proactive
        self._eligibility = eligibility
        self._queue = queue
        self._clock = clock
        self._sleep = sleep
        self._interval = interval
        self._limit = limit

    async def run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception as exc:
                logger.error("proactive sweep failed", extra={"error": type(exc).__name__})
            await self._sleep(self._interval)

    async def sweep(self) -> int:
        rules = self._eligibility.rules
        if switch_reason(rules) is not None:
            return 0
        now = self._clock()
        due = await self._proactive.contacts_due_for_proactive(
            silent_since=now - hours(rules.proactive_after_hours),
            now=now,
            max_per_silence=rules.proactive_max_per_silence,
            interval_since=now - hours(rules.proactive_min_interval_hours),
            limit=self._limit,
        )
        eligible = 0
        enqueued = 0
        failure: str | None = None
        for item in due:
            try:
                check = await self._eligibility.check(item.contact_id)
                last = check.snapshot.tail.last
                if not check.eligible or last is None:
                    continue
                eligible += 1
                await self._queue.enqueue(item.contact_id, last.id)
            except Exception as exc:
                failure = type(exc).__name__
                continue
            enqueued += 1
        extra = {"found": len(due), "eligible": eligible, "enqueued": enqueued}
        if failure is not None:
            logger.warning(
                "proactive sweep could not enqueue every job", extra={**extra, "error": failure}
            )
        elif due:
            logger.info("proactive sweep", extra=extra)
        return enqueued
