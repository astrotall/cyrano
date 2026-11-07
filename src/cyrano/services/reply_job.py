from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from cyrano.domain.autopilot import (
    BURST_HISTORY_MESSAGES,
    PROVIDER_COOLDOWN_SECONDS,
    REPLY_STALE_AFTER_SECONDS,
    SECONDS_PER_MINUTE,
    TRUNCATION_MAX_TOKENS,
    TRUNCATION_RETRY_FACTOR,
    ReplyJobResult,
    retry_delay,
)
from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactStatus,
    MessageDirection,
    OutboxSource,
    ReplyJobOutcome,
    ReplyJobReason,
    TriggerClass,
    TriggerSource,
)
from cyrano.domain.errors import (
    ContactNotFound,
    DefaultModeUnavailable,
    EnqueueRefused,
    LLMAuthError,
    LLMRequestError,
    NoMessagesToReplyTo,
    PersonaMissing,
    PlanNotPossible,
    PromptTooLarge,
    ReplyInvalid,
    RetryJob,
)
from cyrano.domain.models import Message
from cyrano.domain.outbox import CANCEL_REASON_SUPERSEDED
from cyrano.domain.reply import GeneratedReply
from cyrano.services.hand_over import HandOverService
from cyrano.services.humanization import HumanizationPlanner
from cyrano.services.outbox_sender import OWNER_NOTIFY_DEADLINE
from cyrano.services.outbox_service import OutboxService
from cyrano.services.owner_notifier import OwnerNotifier
from cyrano.services.reply_generator import ReplyGenerator
from cyrano.services.repositories import (
    AutopilotStateRepository,
    ContactRepository,
    MessageRepository,
    OutboxRepository,
)
from cyrano.services.send_guard import GuardSettings
from cyrano.services.stop_triggers import detect

logger = logging.getLogger("cyrano.services.reply_job")


@dataclass(frozen=True, slots=True)
class AutopilotNotices:
    stopped: str
    paused: str
    stop_reasons: Mapping[AutopilotStopReason, str]
    pause_reasons: Mapping[AutopilotPauseReason, str]


def _skip(reason: ReplyJobReason, detail: str | None = None) -> ReplyJobResult:
    return ReplyJobResult(outcome=ReplyJobOutcome.SKIPPED, reason=reason, detail=detail)


def burst_texts(history: Sequence[Message], reply_after: datetime | None) -> list[str]:
    texts: list[str] = []
    for message in reversed(history):
        if message.direction is not MessageDirection.INCOMING:
            break
        if reply_after is not None and message.created_at <= reply_after:
            break
        texts.append(message.text)
    texts.reverse()
    return texts


class ReplyJobRunner:
    def __init__(
        self,
        *,
        contacts: ContactRepository,
        messages: MessageRepository,
        outbox: OutboxRepository,
        state: AutopilotStateRepository,
        generator: ReplyGenerator,
        hand_over: HandOverService,
        planner: HumanizationPlanner,
        enqueuer: OutboxService,
        notifier: OwnerNotifier,
        notices: AutopilotNotices,
        switch: GuardSettings,
        max_tokens: int,
        clock: Callable[[], datetime],
        notify_deadline: float = OWNER_NOTIFY_DEADLINE,
    ) -> None:
        self._contacts = contacts
        self._messages = messages
        self._outbox = outbox
        self._state = state
        self._generator = generator
        self._hand_over = hand_over
        self._planner = planner
        self._enqueuer = enqueuer
        self._notifier = notifier
        self._notices = notices
        self._switch = switch
        self._retry_max_tokens = min(max_tokens * TRUNCATION_RETRY_FACTOR, TRUNCATION_MAX_TOKENS)
        self._clock = clock
        self._notify_deadline = notify_deadline

    async def run(
        self, contact_id: int, trigger_message_id: int, *, attempt: int = 1
    ) -> ReplyJobResult:
        try:
            result = await self._steps(contact_id, trigger_message_id)
        except Exception as exc:
            result = await self._transient(exc, attempt, contact_id, trigger_message_id)
        logger.info(
            "reply job finished",
            extra={
                "contact_id": contact_id,
                "trigger_message_id": trigger_message_id,
                "attempt": attempt,
                "outcome": result.outcome.value,
                "reason": result.reason.value,
                "detail": result.detail,
                "row_id": result.row_id,
            },
        )
        return result

    async def _steps(self, contact_id: int, trigger_message_id: int) -> ReplyJobResult:
        now = self._clock()
        if not self._switch.autopilot_enabled:
            return _skip(ReplyJobReason.SWITCH_OFF)
        state = await self._state.get()
        if state.stopped:
            return _skip(ReplyJobReason.AUTOPILOT_STOPPED)
        if state.is_paused(now):
            return _skip(ReplyJobReason.AUTOPILOT_PAUSED)
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            return _skip(ReplyJobReason.CONTACT_MISSING)
        if contact.status is not ContactStatus.ACTIVE:
            return _skip(ReplyJobReason.CONTACT_NOT_ACTIVE)
        trigger = await self._messages.get(trigger_message_id)
        if trigger is None or trigger.contact_id != contact_id:
            return _skip(ReplyJobReason.TRIGGER_MISSING)
        if trigger.direction is not MessageDirection.INCOMING:
            return _skip(ReplyJobReason.TRIGGER_NOT_INCOMING)
        if contact.reply_after is not None and trigger.created_at <= contact.reply_after:
            return _skip(ReplyJobReason.TRIGGER_BEFORE_RESUME)
        if now - trigger.created_at > timedelta(seconds=REPLY_STALE_AFTER_SECONDS):
            return _skip(ReplyJobReason.TRIGGER_STALE)
        history = await self._messages.list_last(contact_id, BURST_HISTORY_MESSAGES)
        if not history:
            return _skip(ReplyJobReason.TRIGGER_MISSING)
        latest = history[-1]
        if latest.id != trigger.id:
            if latest.direction is MessageDirection.INCOMING:
                return _skip(ReplyJobReason.SUPERSEDED)
            return _skip(ReplyJobReason.ANSWERED)
        if await self._outbox.has_row_for_trigger(contact_id, trigger.id):
            return _skip(ReplyJobReason.ROW_EXISTS)
        decision = detect(burst_texts(history, contact.reply_after))
        if decision.triggered:
            return await self._handed_over(
                contact_id, ReplyJobReason.TEXT_TRIGGER, decision.ordered(), TriggerSource.TEXT
            )
        generated = await self._generate(contact_id)
        if isinstance(generated, ReplyJobResult):
            return generated
        return await self._enqueue(contact_id, trigger.id, generated, now)

    async def _generate(self, contact_id: int) -> GeneratedReply | ReplyJobResult:
        max_tokens: int | None = None
        invalid_retried = False
        truncation_retried = False
        while True:
            try:
                reply = await self._generator.generate(contact_id, max_tokens=max_tokens)
            except ReplyInvalid:
                if invalid_retried:
                    return await self._handed_over_by_model(
                        contact_id, ReplyJobReason.REPLY_INVALID
                    )
                invalid_retried = True
                continue
            except PromptTooLarge:
                return await self._handed_over_by_model(contact_id, ReplyJobReason.PROMPT_TOO_LARGE)
            except DefaultModeUnavailable:
                return await self._handed_over_by_model(contact_id, ReplyJobReason.MODE_UNAVAILABLE)
            except NoMessagesToReplyTo:
                return _skip(ReplyJobReason.NO_MESSAGES)
            except ContactNotFound:
                return _skip(ReplyJobReason.CONTACT_MISSING)
            except PersonaMissing as exc:
                return await self._stop(AutopilotStopReason.PERSONA_MISSING, type(exc).__name__)
            except LLMAuthError as exc:
                return await self._stop(AutopilotStopReason.PROVIDER_AUTH, type(exc).__name__)
            except LLMRequestError as exc:
                return await self._stop(AutopilotStopReason.PROVIDER_REQUEST, type(exc).__name__)
            if reply.needs_owner:
                return await self._handed_over_by_model(contact_id, ReplyJobReason.NEEDS_OWNER)
            if reply.truncated:
                if truncation_retried:
                    return await self._handed_over_by_model(contact_id, ReplyJobReason.TRUNCATED)
                truncation_retried = True
                max_tokens = self._retry_max_tokens
                continue
            return reply

    async def _enqueue(
        self, contact_id: int, trigger_id: int, reply: GeneratedReply, now: datetime
    ) -> ReplyJobResult:
        try:
            plan = await self._planner.plan(contact_id, trigger_id, now)
        except PlanNotPossible:
            return _skip(ReplyJobReason.PLAN_NOT_POSSIBLE)
        await self._outbox.cancel_superseded_autopilot_rows(
            contact_id, trigger_id, CANCEL_REASON_SUPERSEDED
        )
        try:
            row = await self._enqueuer.enqueue(
                contact_id, reply.text, plan.send_at, OutboxSource.AUTOPILOT, trigger_id
            )
        except EnqueueRefused as exc:
            return _skip(ReplyJobReason.ENQUEUE_REFUSED, exc.reason)
        return ReplyJobResult(
            outcome=ReplyJobOutcome.QUEUED,
            reason=ReplyJobReason.QUEUED,
            detail=plan.reason.value,
            row_id=row.id,
        )

    async def _handed_over_by_model(
        self, contact_id: int, reason: ReplyJobReason
    ) -> ReplyJobResult:
        return await self._handed_over(contact_id, reason, (), TriggerSource.MODEL)

    async def _handed_over(
        self,
        contact_id: int,
        reason: ReplyJobReason,
        classes: Sequence[TriggerClass],
        source: TriggerSource,
    ) -> ReplyJobResult:
        outcome = await self._hand_over.hand_over(contact_id, classes, source)
        return ReplyJobResult(
            outcome=ReplyJobOutcome.HANDED_OVER, reason=reason, detail=outcome.value
        )

    async def _stop(self, reason: AutopilotStopReason, error: str) -> ReplyJobResult:
        if await self._state.stop(reason):
            await self._notify(
                self._notices.stopped.format(reason=self._notices.stop_reasons[reason], error=error)
            )
        return ReplyJobResult(
            outcome=ReplyJobOutcome.STOPPED, reason=ReplyJobReason(reason.value), detail=error
        )

    async def _transient(
        self, error: Exception, attempt: int, contact_id: int, trigger_message_id: int
    ) -> ReplyJobResult:
        error_class = type(error).__name__
        delay = retry_delay(attempt)
        if delay is not None:
            logger.info(
                "reply job will be retried",
                extra={
                    "contact_id": contact_id,
                    "trigger_message_id": trigger_message_id,
                    "attempt": attempt,
                    "error": error_class,
                    "defer_seconds": delay,
                },
            )
            raise RetryJob(delay, error_class) from None
        try:
            return await self._pause(AutopilotPauseReason.PROVIDER_UNAVAILABLE, error_class)
        except Exception as exc:
            logger.error(
                "autopilot pause failed",
                extra={"contact_id": contact_id, "error": type(exc).__name__},
            )
            return _skip(ReplyJobReason.PROVIDER_UNAVAILABLE, error_class)

    async def _pause(self, reason: AutopilotPauseReason, error: str) -> ReplyJobResult:
        now = self._clock()
        until = now + timedelta(seconds=PROVIDER_COOLDOWN_SECONDS)
        if await self._state.pause_until(until, reason, now=now):
            await self._notify(
                self._notices.paused.format(
                    minutes=PROVIDER_COOLDOWN_SECONDS // SECONDS_PER_MINUTE,
                    reason=self._notices.pause_reasons[reason],
                    error=error,
                )
            )
        return ReplyJobResult(
            outcome=ReplyJobOutcome.PAUSED, reason=ReplyJobReason(reason.value), detail=error
        )

    async def _notify(self, text: str) -> None:
        try:
            async with asyncio.timeout(self._notify_deadline):
                await self._notifier.notify(text)
        except Exception as exc:
            logger.warning("autopilot notice was not sent", extra={"error": type(exc).__name__})
