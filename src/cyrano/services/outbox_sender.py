from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from cyrano.domain.early_read import EarlyReadRequest
from cyrano.domain.enums import (
    ContactStatus,
    EarlyReadOutcome,
    LimitScope,
    LimitWindow,
    OutboxSource,
    PauseReason,
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
    AUTOMATIC_SOURCES,
    CANCEL_REASON_SEND_REJECTED,
    CANCEL_REASON_STALE,
    CANCEL_REASON_SUPERSEDED,
    CAUSE_SEND_DEADLINE,
    ERROR_INTERRUPTED,
    ERROR_OUTSIDE_HOURS,
    ERROR_OUTSIDE_WINDOW,
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
from cyrano.domain.proactive import OUTSIDE_WINDOW_KIND
from cyrano.domain.schedule import MAX_TYPING_SECONDS, ScheduleProfile
from cyrano.services.early_read import EarlyReadGate, EarlyReadQueue
from cyrano.services.humanization import (
    ScheduleService,
    in_proactive_window,
    postponed_after,
    proactive_window_start_after,
    typing_seconds,
)
from cyrano.services.message_transport import MessageTransport
from cyrano.services.owner_notifier import OwnerNotifier
from cyrano.services.repositories import (
    ContactRepository,
    MessageRepository,
    OutboxRepository,
    SenderStateRepository,
)
from cyrano.services.send_guard import SendGuard
from cyrano.services.send_limiter import LimitDecision, LimitHit, SendLimiter

logger = logging.getLogger("cyrano.services.outbox_sender")

SENDER_POLL_INTERVAL = 2.0
SENDER_STALE_CHECK_INTERVAL = 60.0
EARLY_READ_POLL_INTERVAL = 0.5
EARLY_READ_DEADLINE = 15.0
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
EARLY_READ_LOG_MESSAGE = "early read"
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
    early_read_poll_interval: float = EARLY_READ_POLL_INTERVAL
    early_read_deadline: float = EARLY_READ_DEADLINE
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
    flood_wait_early_read: str
    account_stopped: str
    contact_paused: str
    stale_summary: str
    stop_reminder: str
    limit_reached: Mapping[tuple[LimitScope, LimitWindow], str]
    limit_time_format: str


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
        messages: MessageRepository,
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
        early_reads: EarlyReadQueue | None = None,
        early_gate: EarlyReadGate | None = None,
    ) -> None:
        self._outbox = outbox
        self._contacts = contacts
        self._messages = messages
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
        self._early_reads = early_reads
        self._early_gate = early_gate
        self._early_read_counts: Counter[EarlyReadOutcome] = Counter()
        self._limit_notified_at: dict[tuple[LimitScope, LimitWindow], datetime] = {}
        self._stale_unreported = 0
        self._stale_contact_ids: list[int] = []
        self._stale_notified_at: datetime | None = None
        self._stop_noticed_at: datetime | None = None

    async def run(self) -> None:
        await self._step(self.sweep_interrupted)
        await self.poll_forever()

    @property
    def early_read_counts(self) -> dict[EarlyReadOutcome, int]:
        return {outcome: self._early_read_counts[outcome] for outcome in EarlyReadOutcome}

    async def run_early_reads(self) -> None:
        if self._early_reads is None or self._early_gate is None:
            return
        while True:
            await self._step(self.execute_early_reads)
            await self._sleep(self._timings.early_read_poll_interval)

    async def execute_early_reads(self) -> int:
        if self._early_reads is None or self._early_gate is None:
            return 0
        requests = self._early_reads.take_due(self._clock())
        for request in requests:
            await self._execute_early_read(request, self._early_reads, self._early_gate)
        return len(requests)

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
        orphaned = await self._outbox.cancel_orphaned_followers()
        if orphaned:
            logger.info("outbox parts cancelled after their predecessor", extra={"count": orphaned})
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
        follower = row.follows_id is not None
        if (
            row.source in AUTOMATIC_SOURCES
            and not follower
            and await self._hold_outside_hours(row, now)
        ):
            return
        if await self._cancel_if_superseded(row):
            return
        profile = await self._schedule.get_profile()
        try:
            await self._before_send(row, contact.tg_id, profile)
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
        pause = self._rng.uniform(*profile.split_pause_seconds)
        await self._outbox.schedule_followers(row.id, self._clock() + timedelta(seconds=pause))

    async def _is_superseded(self, row: ScheduledMessage) -> bool:
        if row.source not in AUTOMATIC_SOURCES or row.follows_id is not None:
            return False
        if row.trigger_message_id is None:
            return False
        trigger = await self._messages.get(row.trigger_message_id)
        if trigger is None:
            return False
        latest = await self._messages.list_last(row.contact_id, 1)
        return bool(latest) and (latest[0].created_at, latest[0].id) > (
            trigger.created_at,
            trigger.id,
        )

    async def _cancel_if_superseded(self, row: ScheduledMessage) -> bool:
        if not await self._is_superseded(row):
            return False
        await self._outbox.mark_cancelled(row.id, CANCEL_REASON_SUPERSEDED)
        logger.info(
            "outbox row superseded by a newer message",
            extra={"row_id": row.id, "contact_id": row.contact_id},
        )
        return True

    async def _hold_outside_hours(self, row: ScheduledMessage, now: datetime) -> bool:
        block = await self._schedule.blocked_until(now)
        if block is None:
            if row.source is not OutboxSource.PROACTIVE:
                return False
            profile = await self._schedule.get_profile()
            if in_proactive_window(now, profile):
                return False
            end, error, kind = (
                proactive_window_start_after(now, profile),
                ERROR_OUTSIDE_WINDOW,
                OUTSIDE_WINDOW_KIND,
            )
        else:
            profile = await self._schedule.get_profile()
            end, error, kind = block.end, ERROR_OUTSIDE_HOURS, block.kind.value
        send_at = postponed_after(end, profile, self._rng)
        await self._outbox.reschedule(row.id, send_at, error)
        logger.info(
            "outbox row delayed outside the available hours",
            extra={"row_id": row.id, "contact_id": row.contact_id, "kind": kind},
        )
        return True

    async def _before_send(
        self, row: ScheduledMessage, tg_id: int, profile: ScheduleProfile
    ) -> None:
        typing = typing_seconds(row.text, profile)
        pending = [
            _PlannedStep(
                SendStep.TYPING, lambda: self._transport.show_typing(tg_id, typing), True, typing
            )
        ]
        if row.follows_id is None:
            pause = self._rng.uniform(*profile.read_pause_seconds)
            pending[:0] = [
                _PlannedStep(SendStep.MARK_READ, lambda: self._transport.mark_read(tg_id), True),
                _PlannedStep(SendStep.PAUSE, lambda: self._sleep(pause), False, pause),
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
        for hit in limits.hits:
            if hit.limit.window in NOTIFIED_LIMIT_WINDOWS:
                await self._notify_limit(hit, row.contact_id, now)

    async def _notify_limit(self, hit: LimitHit, contact_id: int, now: datetime) -> None:
        kind = (hit.limit.scope, hit.limit.window)
        last = self._limit_notified_at.get(kind)
        interval = timedelta(seconds=self._timings.limit_notice_interval)
        if last is not None and now - last < interval:
            return
        profile = await self._schedule.get_profile()
        self._limit_notified_at[kind] = now
        await self._notify(
            self._notices.limit_reached[kind].format(
                contact_id=contact_id,
                limit=hit.limit.maximum,
                counted=hit.counted,
                free_at=hit.frees_at.astimezone(profile.zone).strftime(
                    self._notices.limit_time_format
                ),
                timezone=profile.timezone,
            )
        )

    async def _handle_failure(self, row: ScheduledMessage, failure: SendFailure) -> None:
        if isinstance(failure, SendFloodWait):
            await self._handle_flood_wait(failure, row.contact_id, row)
        elif isinstance(failure, SendAccountBlocked):
            await self._stop_for_account(failure.kind, row.contact_id, row)
        elif isinstance(failure, SendRejected):
            await self._handle_rejected(row, failure)
        else:
            await self._fail_uncertain(row, failure.error_class)

    async def _handle_flood_wait(
        self, failure: SendFloodWait, contact_id: int, row: ScheduledMessage | None = None
    ) -> None:
        now = self._clock()
        until = now + timedelta(seconds=failure.seconds + self._timings.flood_margin)
        is_new = await self._state.pause_until(until, failure.error_class, now=now)
        extra: dict[str, object] = {"contact_id": contact_id, "seconds": failure.seconds}
        if row is not None:
            await self._outbox.reschedule(row.id, until, failure.error_class)
            extra["row_id"] = row.id
        logger.warning("the sender is paused by a flood wait", extra=extra)
        if is_new and failure.seconds > self._timings.flood_notify_above:
            notice = (
                self._notices.flood_wait if row is not None else self._notices.flood_wait_early_read
            )
            await self._notify(
                notice.format(
                    row_id=None if row is None else row.id,
                    contact_id=contact_id,
                    seconds=failure.seconds,
                )
            )

    async def _stop_for_account(
        self, kind: str, contact_id: int, row: ScheduledMessage | None = None
    ) -> None:
        newly_stopped = await self._state.stop(kind)
        if newly_stopped:
            self._stop_noticed_at = self._clock()
        extra: dict[str, object] = {"contact_id": contact_id, "error": kind}
        if row is not None:
            extra["row_id"] = row.id
        logger.error("the sender is stopped by an account problem", extra=extra)
        try:
            if row is not None:
                await self._outbox.mark_failed(row.id, kind)
        finally:
            if newly_stopped:
                await self._notify(self._notices.account_stopped.format(kind=kind))

    async def _execute_early_read(
        self, request: EarlyReadRequest, queue: EarlyReadQueue, gate: EarlyReadGate
    ) -> None:
        started = self._clock()
        outcome = EarlyReadOutcome.OK
        reason: str | None = None
        error: Exception | None = None
        try:
            verdict = await gate.check(request.contact_id)
            if verdict.contact is None:
                outcome = EarlyReadOutcome.SKIPPED
                reason = None if verdict.reason is None else verdict.reason.value
            else:
                queue.record_executed(request.contact_id, started)
                async with asyncio.timeout(self._timings.early_read_deadline):
                    await self._transport.mark_read(verdict.contact.tg_id)
        except (SendFloodWait, SendAccountBlocked) as exc:
            outcome, error = EarlyReadOutcome.DROPPED, exc
        except Exception as exc:
            outcome, error = EarlyReadOutcome.FAILED, exc
        self._log_early_read(request, outcome, started, reason, error)
        if isinstance(error, SendFloodWait | SendAccountBlocked):
            await self._react_to_early_read_failure(request, error)

    async def _react_to_early_read_failure(
        self, request: EarlyReadRequest, failure: SendFloodWait | SendAccountBlocked
    ) -> None:
        try:
            if isinstance(failure, SendFloodWait):
                await self._handle_flood_wait(failure, request.contact_id)
            else:
                await self._stop_for_account(failure.kind, request.contact_id)
        except Exception as exc:
            logger.error(
                "early read failure handling failed",
                extra={"contact_id": request.contact_id, "error": type(exc).__name__},
            )

    def _log_early_read(
        self,
        request: EarlyReadRequest,
        outcome: EarlyReadOutcome,
        started: datetime,
        reason: str | None,
        error: Exception | None,
    ) -> None:
        self._early_read_counts[outcome] += 1
        extra: dict[str, object] = {
            "contact_id": request.contact_id,
            "outcome": outcome.value,
            "duration_ms": round((self._clock() - started) / STEP_DURATION_UNIT),
        }
        if reason is not None:
            extra["reason"] = reason
        if error is not None:
            extra["error_class"] = type(error).__name__
        logger.info(EARLY_READ_LOG_MESSAGE, extra=extra)

    async def _handle_rejected(self, row: ScheduledMessage, failure: SendRejected) -> None:
        if failure.reason is RejectionReason.RECIPIENT:
            await self._pause_rejected_contact(row, failure.error_class)
        else:
            await self._fail_rejected(row, failure.error_class)

    async def _pause_rejected_contact(self, row: ScheduledMessage, error_class: str) -> None:
        try:
            await self._contacts.set_status(
                row.contact_id, ContactStatus.PAUSED, pause_reason=PauseReason.SEND_REJECTED
            )
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
