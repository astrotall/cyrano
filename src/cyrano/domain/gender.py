from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from cyrano.domain.enums import ContactGender, GenderSource

UNKNOWN_GENDER_LABEL = "unknown"
DEFAULT_GENDER_CHOICES = (UNKNOWN_GENDER_LABEL, *(member.value for member in ContactGender))


class ResolvedGender(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: ContactGender | None = None
    source: GenderSource = GenderSource.UNKNOWN

    @property
    def is_known(self) -> bool:
        return self.value is not None

    @property
    def label(self) -> str:
        return UNKNOWN_GENDER_LABEL if self.value is None else self.value.value


UNRESOLVED_GENDER = ResolvedGender()
