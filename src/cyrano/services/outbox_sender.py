"""The one sender: claims due outbox rows and sends them, one at a time, behind the guard."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from cyrano.domain.enums import (
    ContactStatus,
    LimitScope,
    LimitWindow,
    OutboxSource,
    RejectionReason,
)
from cyrano.domain.errors import (
    InvalidStatusTransition,
    OutboxStateConflict,
    SendAccountBlocked,
    SendFailure,
    SendFloodWait,
    SendRejected,
)
from cyrano.domain.models import Contact
from cyrano.domain.outbox import (
    CANCEL_REASON_SEND_REJECTED,
    CANCEL_REASON_STALE,
    CAUSE_SEND_DEADLINE,
    ERROR_INTERRUPTED,
    ERROR_OUTSIDE_HOURS,
    ERROR_RATE_LIMITED,
    ERROR_UNCERTAIN,
    LIMIT_NOTICE_INTERVAL_SECONDS,
    ROW_STALE_AFTER_SECONDS,
    STALE_NOTICE_CONTACT_IDS_MAX,
    STALE_NOTICE_INTERVAL_SECONDS,
    STOP_REMINDER_INTERVAL_SECONDS,
    ScheduledMessage,
    SenderState,
)
from cyrano.domain.schedule import MAX_TYPING_SECONDS
from cyrano.services.humanization import ScheduleService, postponed_after, typing_seconds
from cyrano.services.message_transport import MessageTransport
from cyrano.services.owner_notifier import OwnerNotifier
from cyrano.services.repositories import (
    ContactRepository,
    OutboxRepository,
    SenderStateRepository,
)
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import LimitDecision, SendLimiter

logger = logging.getLogger("cyrano.services.outbox_sender")

SENDER_POLL_INTERVAL = 2.0
SENDER_STALE_CHECK_INTERVAL = 60.0
SENDER_SEND_DEADLINE = 100.0
PRE_SEND_DEADLINE_MARGIN = 15.0
LONGEST_PRE_SEND = MAX_TYPING_SECONDS + PRE_SEND_DEADLINE_MARGIN
SENDING_STALE_MARGIN = 20.0
SENDING_STALE_AFTER = LONGEST_PRE_SEND + SENDER_SEND_DEADLINE + SENDING_STALE_MARGIN
FLOOD_WAIT_MARGIN = 5.0
FLOOD_WAIT_NOTIFY_ABOVE = 60.0
OWNER_NOTIFY_DEADLINE = 15.0
NOTIFIED_LIMIT_WINDOWS = frozenset({LimitWindow.HOUR, LimitWindow.DAY})
STEP_LOG_MESSAGE = "outbox row step"
STEP_DURATION_UNIT = timedelta(milliseconds=1)
_SYSTEM_RANDOM = random.SystemRandom()


class SendStep(StrEnum):
    MARK_READ = "mark_read"
    PAUSE = "pause"
    TYPING = "typing"
    SEND = "send"


class StepOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SenderTimings:
    poll_interval: float = SENDER_POLL_INTERVAL
    stale_check_interval: float = SENDER_STALE_CHECK_INTERVAL
    stale_after: float = SENDING_STALE_AFTER
    send_deadline: float = SENDER_SEND_DEADLINE
    pre_send_margin: float = PRE_SEND_DEADLINE_MARGIN
    flood_margin: float = FLOOD_WAIT_MARGIN
    flood_notify_above: float = FLOOD_WAIT_NOTIFY_ABOVE
    row_stale_after: float = ROW_STALE_AFTER_SECONDS
    limit_notice_interval: float = LIMIT_NOTICE_INTERVAL_SECONDS
    stale_notice_interval: float = STALE_NOTICE_INTERVAL_SECONDS
    stop_reminder_interval: float = STOP_REMINDER_INTERVAL_SECONDS
    notify_deadline: float = OWNER_NOTIFY_DEADLINE


@dataclass(frozen=True, slots=True)
class SenderNotices:
    interrupted: str
    failed: str
    uncertain: str
    flood_wait: str
    account_stopped: str
    contact_paused: str
    stale_summary: str
    stop_reminder: str
    limit_reached: Mapping[tuple[LimitScope, LimitWindow], str]


@dataclass(frozen=True, slots=True)
class _PlannedStep:
    step: SendStep
    run: Callable[[], Awaitable[None]]
    cosmetic: bool
    planned_seconds: float | None = None


class OutboxSender:
    def __init__(
        self,
        outbox: OutboxRepository,
        contacts: ContactRepository,
        state: SenderStateRepository,
        limiter: SendLimiter,
        schedule: ScheduleService,
        transport: MessageTransport,
        notifier: OwnerNotifier,
        guard: SendGuard,
        notices: SenderNotices,
        *,
        clock: Callable[[], datetime] = _utc_now,
        timings: SenderTimings | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random = _SYSTEM_RANDOM,
    ) -> None:
        self._outbox = outbox
        self._contacts = contacts
        self._state = state
        self._limiter = limiter
        self._schedule = schedule
        self._transport = transport
        self._notifier = notifier
        self._guard = guard
        self._notices = notices
        self._clock = clock
        self._timings = timings if timings is not None else SenderTimings()
        self._sleep = sleep
        self._rng = rng
        self._limit_notified_at: dict[tuple[LimitScope, LimitWindow], datetime] = {}
        self._stale_unreported = 0
        self._stale_contact_ids: list[int] = []
        self._stale_notified_at: datetime | None = None
        self._stop_noticed_at: datetime | None = None

    async def run(self) -> None:
        await self._step(self.sweep_interrupted)
        await self.poll_forever()

    async def poll_forever(self) -> None:
        stale_every = timedelta(seconds=self._timings.stale_check_interval)
        next_stale_check = self._clock() + stale_every
        while True:
            if self._clock() >= next_stale_check:
                next_stale_check = self._clock() + stale_every
                await self._step(self.fail_stale)
            await self._step(self.send_next)
            await self._sleep(self._timings.poll_interval)

    async def sweep_interrupted(self) -> None:
        await self._fail_interrupted(self._clock())

    async def fail_stale(self) -> None:
        await self._fail_interrupted(self._clock() - timedelta(seconds=self._timings.stale_after))

    async def send_next(self) -> bool:
        now = self._clock()
        await self._report_stale(now)
        state = await self._state.get()
        await self._remind_about_stop(state, now)
        if state.stopped or state.is_paused(now):
            return False
        row = await self._outbox.claim_due(now)
        if row is None:
            return False
        await self._deliver(row)
        return True

    async def _step(self, step: Callable[[], Awaitable[object]]) -> None:
        try:
            await step()
        except Exception as exc:
            logger.error("outbox sender step failed", extra={"error": type(exc).__name__})

    async def _fail_interrupted(self, claimed_before: datetime) -> None:
        for row in await self._outbox.stale_sending(claimed_before):
            try:
                await self._outbox.mark_failed(row.id, ERROR_INTERRUPTED)
            except OutboxStateConflict:
                continue
            logger.warning(
                "outbox row interrupted", extra={"row_id": row.id, "contact_id": row.contact_id}
            )
            await self._notify(
                self._notices.interrupted.format(
                    row_id=row.id, contact_id=row.contact_id, error=ERROR_INTERRUPTED
                )
            )

    async def _guarded_contact(self, row: ScheduledMessage) -> Contact | None:
        contact = await self._contacts.get_by_id(row.contact_id)
        decision = self._guard.check(contact, row.source)
        if contact is None or not decision.allowed:
            await self._outbox.mark_cancelled(row.id, decision.reason)
            logger.info(
                "outbox row refused by the guard",
                extra={"row_id": row.id, "contact_id": row.contact_id, "reason": decision.reason},
            )
            return None
        return contact

    async def _deliver(self, row: ScheduledMessage) -> None:
        now = self._clock()
        if now - row.due_at > timedelta(seconds=self._timings.row_stale_after):
            await self._outbox.mark_cancelled(row.id, CANCEL_REASON_STALE)
            logger.warning(
                "outbox row is stale and was cancelled",
                extra={"row_id": row.id, "contact_id": row.contact_id},
            )
            self._count_stale(row.contact_id)
            await self._report_stale(now)
            return
        contact = await self._guarded_contact(row)
        if contact is None:
            return
        limits = await self._limiter.check(row.contact_id, now)
        if limits.free_at is not None:
            await self._hold_for_limit(row, limits, limits.free_at, now)
            return
        if row.source is OutboxSource.AUTOPILOT and await self._hold_outside_hours(row, now):
            return
        try:
            await self._before_send(row, contact.tg_id)
        except (SendFloodWait, SendAccountBlocked) as exc:
            self._log_step(row, SendStep.SEND, StepOutcome.SKIPPED)
            await self._handle_failure(row, exc)
            return
        contact = await self._guarded_contact(row)
        if contact is None:
            self._log_step(row, SendStep.SEND, StepOutcome.SKIPPED)
            return
        deadline = asyncio.timeout(self._timings.send_deadline)
        started = self._clock()
        try:
            async with deadline:
                sent = await self._transport.send_text(contact.tg_id, row.text)
        except SendFailure as exc:
            self._log_step(row, SendStep.SEND, StepOutcome.FAILED, started, error=exc)
            await self._handle_failure(row, exc)
            return
        except Exception as exc:
            if deadline.expired():
                self._log_step(row, SendStep.SEND, StepOutcome.TIMEOUT, started)
                cause = CAUSE_SEND_DEADLINE
            else:
                self._log_step(row, SendStep.SEND, StepOutcome.FAILED, started, error=exc)
                cause = type(exc).__name__
            await self._fail_uncertain(row, cause)
            return
        self._log_step(row, SendStep.SEND, StepOutcome.OK, started)
        await self._outbox.complete_send(
            row.id, row.contact_id, row.text, sent.tg_message_id, sent.sent_at
        )
        logger.info("outbox row sent", extra={"row_id": row.id, "contact_id": row.contact_id})

    async def _hold_outside_hours(self, row: ScheduledMessage, now: datetime) -> bool:
        block = await self._schedule.blocked_until(now)
        if block is None:
            return False
        profile = await self._schedule.get_profile()
        send_at = postponed_after(block.end, profile, self._rng)
        await self._outbox.reschedule(row.id, send_at, ERROR_OUTSIDE_HOURS)
        logger.info(
            "outbox row delayed outside the available hours",
            extra={"row_id": row.id, "contact_id": row.contact_id, "kind": block.kind.value},
        )
        return True

    async def _before_send(self, row: ScheduledMessage, tg_id: int) -> None:
        profile = await self._schedule.get_profile()
        pause = self._rng.uniform(*profile.read_pause_seconds)
        typing = typing_seconds(row.text, profile)
        pending = [
            _PlannedStep(SendStep.MARK_READ, lambda: self._transport.mark_read(tg_id), True),
            _PlannedStep(SendStep.PAUSE, lambda: self._sleep(pause), False, pause),
            _PlannedStep(
                SendStep.TYPING, lambda: self._transport.show_typing(tg_id, typing), True, typing
            ),
        ]
        started = self._clock()
        try:
            async with asyncio.timeout(profile.typing.max_seconds + self._timings.pre_send_margin):
                while pending:
                    started = self._clock()
                    await self._pre_send_step(row, pending[0], started)
                    pending.pop(0)
        except TimeoutError:
            logger.info(
                "the pre-send part ran out of time",
                extra={"row_id": row.id, "contact_id": row.contact_id},
            )
            if pending:
                overran, *pending = pending
                self._log_planned(row, overran, StepOutcome.TIMEOUT, started)
            self._log_skipped(row, pending)
        except SendFloodWait, SendAccountBlocked:
            self._log_skipped(row, pending[1:])
            raise

    async def _pre_send_step(
        self, row: ScheduledMessage, planned: _PlannedStep, started: datetime
    ) -> None:
        try:
            await planned.run()
        except (SendFloodWait, SendAccountBlocked) as exc:
            self._log_planned(row, planned, StepOutcome.FAILED, started, error=exc)
            raise
        except Exception as exc:
            self._log_planned(row, planned, StepOutcome.FAILED, started, error=exc)
            if not planned.cosmetic:
                raise
            logger.info("a pre-send step failed", extra={"error": type(exc).__name__})
            return
        self._log_planned(row, planned, StepOutcome.OK, started)

    def _log_skipped(self, row: ScheduledMessage, steps: list[_PlannedStep]) -> None:
        for planned in steps:
            self._log_planned(row, planned, StepOutcome.SKIPPED)

    def _log_planned(
        self,
        row: ScheduledMessage,
        planned: _PlannedStep,
        outcome: StepOutcome,
        started: datetime | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._log_step(
            row,
            planned.step,
            outcome,
            started,
            error=error,
            planned_seconds=planned.planned_seconds,
        )

    def _log_step(
        self,
        row: ScheduledMessage,
        step: SendStep,
        outcome: StepOutcome,
        started: datetime | None = None,
        *,
        error: Exception | None = None,
        planned_seconds: float | None = None,
    ) -> None:
        duration = timedelta() if started is None else self._clock() - started
        extra: dict[str, object] = {
            "row_id": row.id,
            "contact_id": row.contact_id,
            "step": step.value,
            "outcome": outcome.value,
            "duration_ms": round(duration / STEP_DURATION_UNIT),
        }
        if error is not None:
            extra["error_class"] = type(error).__name__
        if planned_seconds is not None:
            extra["planned_seconds"] = planned_seconds
        logger.info(STEP_LOG_MESSAGE, extra=extra)

    async def _hold_for_limit(
        self, row: ScheduledMessage, limits: LimitDecision, free_at: datetime, now: datetime
    ) -> None:
        await self._outbox.reschedule(row.id, free_at, ERROR_RATE_LIMITED)
        logger.info(
            "outbox row delayed by a send limit",
            extra={
                "row_id": row.id,
                "contact_id": row.contact_id,
                "limits": [f"{limit.scope}_{limit.window}" for limit in limits.reached],
            },
        )
        for limit in limits.reached:
            if limit.window in NOTIFIED_LIMIT_WINDOWS:
                await self._notify_limit(limit.scope, limit.window, row.contact_id, now)

    async def _notify_limit(
        self, scope: LimitScope, window: LimitWindow, contact_id: int, now: datetime
    ) -> None:
        last = self._limit_notified_at.get((scope, window))
        interval = timedelta(seconds=self._timings.limit_notice_interval)
        if last is not None and now - last < interval:
            return
        self._limit_notified_at[(scope, window)] = now
        await self._notify(
            self._notices.limit_reached[(scope, window)].format(contact_id=contact_id)
        )

    async def _handle_failure(self, row: ScheduledMessage, failure: SendFailure) -> None:
        if isinstance(failure, SendFloodWait):
            await self._handle_flood_wait(row, failure)
        elif isinstance(failure, SendAccountBlocked):
            await self._stop_for_account(row, failure.kind)
        elif isinstance(failure, SendRejected):
            await self._handle_rejected(row, failure)
        else:
            await self._fail_uncertain(row, failure.error_class)

    async def _handle_flood_wait(self, row: ScheduledMessage, failure: SendFloodWait) -> None:
        now = self._clock()
        until = now + timedelta(seconds=failure.seconds + self._timings.flood_margin)
        is_new = await self._state.pause_until(until, failure.error_class, now=now)
        await self._outbox.reschedule(row.id, until, failure.error_class)
        logger.warning(
            "the sender is paused by a flood wait",
            extra={"row_id": row.id, "contact_id": row.contact_id, "seconds": failure.seconds},
        )
        if is_new and failure.seconds > self._timings.flood_notify_above:
            await self._notify(
                self._notices.flood_wait.format(
                    row_id=row.id, contact_id=row.contact_id, seconds=failure.seconds
                )
            )

    async def _stop_for_account(self, row: ScheduledMessage, kind: str) -> None:
        newly_stopped = await self._state.stop(kind)
        if newly_stopped:
            self._stop_noticed_at = self._clock()
        logger.error(
            "the sender is stopped by an account problem",
            extra={"row_id": row.id, "contact_id": row.contact_id, "error": kind},
        )
        try:
            await self._outbox.mark_failed(row.id, kind)
        finally:
            if newly_stopped:
                await self._notify(self._notices.account_stopped.format(kind=kind))

    async def _handle_rejected(self, row: ScheduledMessage, failure: SendRejected) -> None:
        if failure.reason is RejectionReason.RECIPIENT:
            await self._pause_rejected_contact(row, failure.error_class)
        else:
            await self._fail_rejected(row, failure.error_class)

    async def _pause_rejected_contact(self, row: ScheduledMessage, error_class: str) -> None:
        try:
            await self._contacts.set_status(row.contact_id, ContactStatus.PAUSED)
        except InvalidStatusTransition as exc:
            logger.info(
                "the rejected contact cannot be paused",
                extra={"contact_id": row.contact_id, "status": exc.current},
            )
        await self._outbox.cancel_pending_for_contact(row.contact_id, CANCEL_REASON_SEND_REJECTED)
        await self._outbox.mark_failed(row.id, error_class)
        logger.warning(
            "outbox row failed and its contact is paused",
            extra={"row_id": row.id, "contact_id": row.contact_id, "error": error_class},
        )
        await self._notify(
            self._notices.contact_paused.format(contact_id=row.contact_id, error=error_class)
        )

    async def _fail_rejected(self, row: ScheduledMessage, error_class: str) -> None:
        await self._outbox.mark_failed(row.id, error_class)
        logger.warning(
            "outbox row failed",
            extra={"row_id": row.id, "contact_id": row.contact_id, "error": error_class},
        )
        await self._notify(
            self._notices.failed.format(row_id=row.id, contact_id=row.contact_id, error=error_class)
        )

    async def _fail_uncertain(self, row: ScheduledMessage, cause: str) -> None:
        await self._outbox.mark_failed(row.id, ERROR_UNCERTAIN)
        logger.warning(
            "outbox row outcome is unknown",
            extra={"row_id": row.id, "contact_id": row.contact_id, "error": cause},
        )
        await self._notify(self._notices.uncertain.format(row_id=row.id, contact_id=row.contact_id))

    def _count_stale(self, contact_id: int) -> None:
        self._stale_unreported += 1
        if (
            contact_id not in self._stale_contact_ids
            and len(self._stale_contact_ids) < STALE_NOTICE_CONTACT_IDS_MAX
        ):
            self._stale_contact_ids.append(contact_id)

    async def _report_stale(self, now: datetime) -> None:
        if self._stale_unreported == 0:
            return
        last = self._stale_notified_at
        if last is not None and now - last < timedelta(seconds=self._timings.stale_notice_interval):
            return
        text = self._notices.stale_summary.format(
            count=self._stale_unreported,
            contact_ids=", ".join(f"#{contact_id}" for contact_id in self._stale_contact_ids),
        )
        self._stale_unreported = 0
        self._stale_contact_ids = []
        self._stale_notified_at = now
        await self._notify(text)

    async def _remind_about_stop(self, state: SenderState, now: datetime) -> None:
        if not state.stopped:
            self._stop_noticed_at = None
            return
        if self._stop_noticed_at is None:
            self._stop_noticed_at = now
            return
        if now - self._stop_noticed_at < timedelta(seconds=self._timings.stop_reminder_interval):
            return
        self._stop_noticed_at = now
        await self._notify(self._notices.stop_reminder.format(kind=state.stop_reason))

    async def _notify(self, text: str) -> None:
        try:
            async with asyncio.timeout(self._timings.notify_deadline):
                await self._notifier.notify(text)
        except Exception as exc:
            logger.warning("owner notification failed", extra={"error": type(exc).__name__})
