from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from cyrano.llm.constants import ROOT_PATH_LABEL
from cyrano.llm.prompts import PROBLEM_LINE

FENCED_JSON_PATTERN = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class JsonProblems:
    lines: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.lines)


def extract_json_text(text: str) -> str:
    fenced = FENCED_JSON_PATTERN.search(text)
    return fenced.group(1) if fenced is not None else text


def _problem_lines(error: ValidationError) -> tuple[str, ...]:
    return tuple(
        PROBLEM_LINE.format(
            path=".".join(str(part) for part in item["loc"]) or ROOT_PATH_LABEL,
            message=item["msg"],
        )
        for item in error.errors(include_input=False, include_url=False, include_context=False)
    )


def parse_json_reply[T: BaseModel](text: str, schema: type[T]) -> T | JsonProblems:
    try:
        return schema.model_validate_json(extract_json_text(text))
    except ValidationError as error:
        return JsonProblems(lines=_problem_lines(error))
