from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
)
from pydantic_core import ErrorDetails

from cyrano.domain.errors import ContentProblem, ContentValidationError, ImportFileError
from cyrano.domain.style import (
    MAX_DIALOGUES_PER_FILE,
    TAG_RULE,
    PersonaCreate,
    StyleExampleCreate,
)

FILE_FORMAT_VERSION = 1
MAX_IMPORT_FILE_BYTES = 5 * 1024 * 1024
ROOT_PATH = "(file)"
PERSONA_SUBJECT = "persona file"
EXAMPLES_SUBJECT = "style examples file"
VERSION_RULE = f"must be {FILE_FORMAT_VERSION}"
STATIC_RULES: Mapping[str, str] = {
    "missing": "missing",
    "extra_forbidden": "unknown key",
    "string_too_short": "empty",
    "too_short": "empty",
    "string_pattern_mismatch": TAG_RULE,
    "string_type": "must be text",
    "list_type": "must be a list",
    "tuple_type": "must be a list",
    "dict_type": "must be an object",
    "model_type": "must be an object",
    "int_type": "must be an integer",
}
INVALID_RULE = "invalid"


class ExamplesFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    version: StrictInt
    dialogues: Annotated[
        list[StyleExampleCreate], Field(min_length=1, max_length=MAX_DIALOGUES_PER_FILE)
    ]

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        if value != FILE_FORMAT_VERSION:
            raise ValueError(VERSION_RULE)
        return value


def read_import_file(path: Path, *, max_bytes: int = MAX_IMPORT_FILE_BYTES) -> str:
    try:
        is_file = path.is_file()
    except OSError as exc:
        raise ImportFileError(f"cannot read {path}: {type(exc).__name__}") from None
    if not is_file:
        raise ImportFileError(f"not a readable file: {path}")
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise ImportFileError(f"cannot read {path}: {type(exc).__name__}") from None
    if len(data) > max_bytes:
        raise ImportFileError(f"file is larger than {max_bytes} bytes: {path}")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFileError(f"file is not valid UTF-8: {path}") from None


def _path(location: Sequence[int | str]) -> str:
    rendered = ""
    for part in location:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}" if rendered else part
    return rendered or ROOT_PATH


def _rule(error: ErrorDetails) -> str:
    kind = error["type"]
    context = error.get("ctx", {})
    if kind in STATIC_RULES:
        return STATIC_RULES[kind]
    if kind == "string_too_long":
        return f"too long (max {context['max_length']})"
    if kind == "too_long":
        return f"too many (max {context['max_length']})"
    if kind == "enum":
        return f"must be {context['expected']}"
    if kind == "greater_than_equal":
        return f"must be at least {context['ge']}"
    if kind == "less_than_equal":
        return f"must be at most {context['le']}"
    if kind == "value_error":
        return str(context["error"])
    return INVALID_RULE


def problems_from(error: ValidationError, *, skip: int = 0) -> list[ContentProblem]:
    problems: list[ContentProblem] = []
    for detail in error.errors(include_input=False):
        location = detail["loc"][skip:]
        if detail["type"] == "extra_forbidden":
            location = location[:-1]
        problems.append(ContentProblem(_path(location), _rule(detail)))
    return problems


def parse_persona(raw: str) -> PersonaCreate:
    try:
        return PersonaCreate(text=raw)
    except ValidationError as exc:
        raise ContentValidationError(PERSONA_SUBJECT, problems_from(exc, skip=1)) from None


def load_json_document(raw: str, subject: str) -> object:
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        rule = f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        raise ContentValidationError(subject, [ContentProblem(ROOT_PATH, rule)]) from None
    except RecursionError:
        raise ContentValidationError(
            subject, [ContentProblem(ROOT_PATH, "nested too deeply")]
        ) from None
    return data


def parse_examples(raw: str) -> list[StyleExampleCreate]:
    data = load_json_document(raw, EXAMPLES_SUBJECT)
    try:
        return list(ExamplesFile.model_validate(data).dialogues)
    except ValidationError as exc:
        raise ContentValidationError(EXAMPLES_SUBJECT, problems_from(exc)) from None
