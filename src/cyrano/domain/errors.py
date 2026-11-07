"""Domain errors. Messages carry internal ids and statuses only, never personal data."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cyrano.domain.enums import ContactStatus, OutboxState, RejectionReason


class DomainError(Exception):
    pass


class InvalidStatusTransition(DomainError):
    def __init__(self, current: ContactStatus, requested: ContactStatus) -> None:
        super().__init__(f"status transition {current} -> {requested} is not allowed")
        self.current = current
        self.requested = requested


class ActivationWithoutMode(InvalidStatusTransition):
    def __init__(self, current: ContactStatus) -> None:
        super().__init__(current, ContactStatus.ACTIVE)


class PauseReasonRefused(DomainError):
    def __init__(self, status: ContactStatus) -> None:
        super().__init__(f"a pause reason is allowed only with the paused status, not {status}")
        self.status = status


class ReplyAfterRefused(DomainError):
    def __init__(self, status: ContactStatus) -> None:
        super().__init__(f"a reply-after time is allowed only with the active status, not {status}")
        self.status = status


class ContactNotFound(DomainError):
    def __init__(self, contact_id: int) -> None:
        super().__init__(f"contact {contact_id} does not exist")
        self.contact_id = contact_id


class ContactAlreadyExists(DomainError):
    def __init__(self) -> None:
        super().__init__("a contact with this Telegram id already exists")


class ModeNotFound(DomainError):
    def __init__(self, mode_id: int) -> None:
        super().__init__(f"mode {mode_id} does not exist")
        self.mode_id = mode_id


class ModeAlreadyExists(DomainError):
    def __init__(self, name: str) -> None:
        super().__init__(f"mode {name!r} already exists")
        self.name = name


class DefaultModeUnavailable(DomainError):
    def __init__(self) -> None:
        super().__init__("the default mode is missing or disabled")


class LLMError(DomainError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMAuthError(LLMError):
    pass


class LLMRequestError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMUnavailableError(LLMError):
    pass


class LLMInvalidResponseError(LLMError):
    pass


MAX_REPORTED_PROBLEMS = 20


@dataclass(frozen=True)
class ContentProblem:
    path: str
    rule: str


class ImportFileError(DomainError):
    pass


class ChatExportError(DomainError):
    pass


class ContentValidationError(DomainError):
    def __init__(self, subject: str, problems: Sequence[ContentProblem]) -> None:
        shown = problems[:MAX_REPORTED_PROBLEMS]
        header = f"{subject} is invalid: {len(problems)} problem(s)"
        if len(problems) > len(shown):
            header += f", showing the first {len(shown)}"
        lines = [header, *(f"  {problem.path}: {problem.rule}" for problem in shown)]
        super().__init__("\n".join(lines))
        self.problems = tuple(problems)


class PersonaMissing(DomainError):
    def __init__(self) -> None:
        super().__init__("no persona is stored")


class ModeNameNotFound(DomainError):
    def __init__(self) -> None:
        super().__init__("the requested mode does not exist")


class NoMessagesToReplyTo(DomainError):
    def __init__(self, contact_id: int) -> None:
        super().__init__(f"contact {contact_id} has no incoming message to reply to")
        self.contact_id = contact_id


class PromptTooLarge(DomainError):
    def __init__(self, characters: int, budget: int) -> None:
        super().__init__(
            f"the prompt has {characters} characters at its smallest, over the budget of {budget}"
        )
        self.characters = characters
        self.budget = budget


class ReplyInvalid(DomainError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"the model reply is unusable: {reason}")
        self.reason = reason


class EnqueueRefused(DomainError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"the message was not enqueued: {reason}")
        self.reason = reason


class OutboxRowNotFound(DomainError):
    def __init__(self, row_id: int) -> None:
        super().__init__(f"outbox row {row_id} does not exist")
        self.row_id = row_id


class OutboxRowInvalid(DomainError):
    def __init__(self) -> None:
        super().__init__("the outbox row breaks a table constraint")


class OutboxStateConflict(DomainError):
    def __init__(self, row_id: int, state: OutboxState) -> None:
        super().__init__(f"outbox row {row_id} is in state {state}")
        self.row_id = row_id
        self.state = state


class SenderStateMissing(DomainError):
    def __init__(self) -> None:
        super().__init__("the sender state row is missing; run the migrations")


class SendFailure(DomainError):
    def __init__(self, error_class: str) -> None:
        super().__init__(f"the send failed: {error_class}")
        self.error_class = error_class


class SendRejected(SendFailure):
    def __init__(self, error_class: str, reason: RejectionReason) -> None:
        super().__init__(error_class)
        self.reason = reason


class SendAccountBlocked(SendFailure):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class SendUncertain(SendFailure):
    pass


class SendFloodWait(SendFailure):
    def __init__(self, error_class: str, seconds: int) -> None:
        super().__init__(error_class)
        self.seconds = seconds


class PlanNotPossible(DomainError):
    def __init__(self, contact_id: int, message_id: int) -> None:
        super().__init__(
            f"message {message_id} of contact {contact_id} is not an incoming message "
            "in the recent history"
        )
        self.contact_id = contact_id
        self.message_id = message_id


class StoredScheduleInvalid(DomainError):
    def __init__(self) -> None:
        super().__init__("the stored schedule profile is invalid; import it again")


class ScheduleTimezoneUnresolved(DomainError):
    def __init__(self) -> None:
        super().__init__("the schedule profile has no time zone; read it through ScheduleService")


class AutopilotStateMissing(DomainError):
    def __init__(self) -> None:
        super().__init__("the autopilot state row is missing; run the migrations")


class RetryJob(DomainError):
    def __init__(self, defer_seconds: int, error_class: str) -> None:
        super().__init__(f"the reply job will be retried in {defer_seconds} s after {error_class}")
        self.defer_seconds = defer_seconds
        self.error_class = error_class
