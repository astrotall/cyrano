from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta

from cyrano.domain.autopilot import ReplyJobResult
from cyrano.domain.enums import (
    OutboxSource,
    ProactiveJobOutcome,
    ProactiveJobReason,
    ReplyPurpose,
)
from cyrano.domain.errors import EnqueueRefused
from cyrano.domain.proactive import (
    FAILURE_POSTPONE_SECONDS,
    MODEL_SKIP_POSTPONE_SECONDS,
    ProactiveJobResult,
)
from cyrano.services.humanization import ScheduleService, plan_proactive_time
from cyrano.services.outbox_service import OutboxService
from cyrano.services.proactive_eligibility import ProactiveEligibility, hours
from cyrano.services.reply_job import GenerationPolicy
from cyrano.services.reply_split import split_reply
from cyrano.services.repositories import OutboxRepository, ProactiveRepository

logger = logging.getLogger("cyrano.services.proactive_job")

PROACTIVE_RETRY_MESSAGE = "proactive job will be retried"
FAILED_OUTCOMES = frozenset({ProactiveJobOutcome.STOPPED, ProactiveJobOutcome.PAUSED})


def _skip(reason: ProactiveJobReason, detail: str | None = None) -> ProactiveJobResult:
    return ProactiveJobResult(outcome=ProactiveJobOutcome.SKIPPED, reason=reason, detail=detail)


def _from_reply_result(result: ReplyJobResult) -> ProactiveJobResult:
    return ProactiveJobResult(
        outcome=ProactiveJobOutcome(result.outcome.value),
        reason=ProactiveJobReason(result.reason.value),
        detail=result.detail,
    )


class ProactiveJobRunner:
    def __init__(
        self,
        *,
        eligibility: ProactiveEligibility,
        proactive: ProactiveRepository,
        outbox: OutboxRepository,
        policy: GenerationPolicy,
        schedule: ScheduleService,
        enqueuer: OutboxService,
        rng: random.Random,
        clock: Callable[[], datetime],
    ) -> None:
        self._eligibility = eligibility
        self._proactive = proactive
        self._outbox = outbox
        self._policy = policy
        self._schedule = schedule
        self._enqueuer = enqueuer
        self._rng = rng
        self._clock = clock

    async def run(
        self, contact_id: int, last_message_id: int, *, attempt: int = 1
    ) -> ProactiveJobResult:
        try:
            result = await self._steps(contact_id, last_message_id)
        except Exception as exc:
            result = _from_reply_result(
                await self._policy.transient(
                    exc,
                    attempt,
                    contact_id,
                    {"last_message_id": last_message_id},
                    retry_message=PROACTIVE_RETRY_MESSAGE,
                )
            )
        if result.outcome in FAILED_OUTCOMES:
            await self._postpone(contact_id, FAILURE_POSTPONE_SECONDS)
        logger.info(
            "proactive job finished",
            extra={
                "contact_id": contact_id,
                "last_message_id": last_message_id,
                "attempt": attempt,
                "outcome": result.outcome.value,
                "reason": result.reason.value,
                "detail": result.detail,
                "row_id": result.row_id,
                "parts": result.parts,
            },
        )
        return result

    async def _steps(self, contact_id: int, last_message_id: int) -> ProactiveJobResult:
        now = self._clock()
        check = await self._eligibility.check(contact_id)
        if not check.eligible:
            return _skip(ProactiveJobReason(check.reason.value))
        last = check.snapshot.tail.last
        if last is None or last.id != last_message_id:
            return _skip(ProactiveJobReason.SUPERSEDED)
        if await self._outbox.has_row_for_trigger(contact_id, last_message_id):
            return _skip(ProactiveJobReason.ROW_EXISTS)
        generated = await self._policy.generate(contact_id, purpose=ReplyPurpose.PROACTIVE)
        if isinstance(generated, ReplyJobResult):
            return _from_reply_result(generated)
        if generated.skipped:
            await self._proactive.set_proactive_not_before(
                contact_id, now + timedelta(seconds=MODEL_SKIP_POSTPONE_SECONDS)
            )
            return _skip(ProactiveJobReason.MODEL_SKIP)
        plan = plan_proactive_time(now, await self._schedule.get_profile(), self._rng)
        try:
            rows = await self._enqueuer.enqueue_sequence(
                contact_id,
                split_reply(generated.text),
                plan.send_at,
                OutboxSource.PROACTIVE,
                last_message_id,
            )
        except EnqueueRefused as exc:
            return _skip(ProactiveJobReason.ENQUEUE_REFUSED, exc.reason)
        await self._proactive.set_proactive_not_before(
            contact_id, now + hours(self._eligibility.rules.proactive_min_interval_hours)
        )
        return ProactiveJobResult(
            outcome=ProactiveJobOutcome.QUEUED,
            reason=ProactiveJobReason.QUEUED,
            detail=plan.reason.value,
            row_id=rows[0].id,
            parts=len(rows),
        )

    async def _postpone(self, contact_id: int, seconds: int) -> None:
        try:
            await self._proactive.set_proactive_not_before(
                contact_id, self._clock() + timedelta(seconds=seconds)
            )
        except Exception as exc:
            logger.warning(
                "proactive postponement was not stored",
                extra={"contact_id": contact_id, "error": type(exc).__name__},
            )
