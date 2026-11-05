"""Reading and validating the schedule file, and storing it; validation comes first."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from cyrano.domain.errors import ContentValidationError
from cyrano.domain.schedule import ScheduleProfile
from cyrano.services.repositories import ScheduleProfileRepository
from cyrano.services.style_files import load_json_document, problems_from, read_import_file

logger = logging.getLogger("cyrano.services.schedule_files")

MAX_SCHEDULE_FILE_BYTES = 64 * 1024
SCHEDULE_SUBJECT = "schedule file"


def parse_schedule(raw: str) -> ScheduleProfile:
    data = load_json_document(raw, SCHEDULE_SUBJECT)
    try:
        return ScheduleProfile.model_validate(data)
    except ValidationError as exc:
        raise ContentValidationError(SCHEDULE_SUBJECT, problems_from(exc)) from None


@dataclass(frozen=True)
class ScheduleImportResult:
    busy_windows: int


class ScheduleImporter:
    def __init__(self, repository: ScheduleProfileRepository) -> None:
        self._repository = repository

    async def import_file(self, path: Path) -> ScheduleImportResult:
        profile = parse_schedule(read_import_file(path, max_bytes=MAX_SCHEDULE_FILE_BYTES))
        stored = await self._repository.replace(profile)
        logger.info("schedule profile imported", extra={"busy_windows": len(stored.busy)})
        return ScheduleImportResult(busy_windows=len(stored.busy))
