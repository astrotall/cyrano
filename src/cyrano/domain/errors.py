"""Domain errors. Messages carry internal ids and statuses only, never personal data."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cyrano.domain.enums import ContactStatus


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
