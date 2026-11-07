from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cyrano.domain.enums import Role


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LLMMessage(_Frozen):
    role: Role
    content: str


class LLMUsage(_Frozen):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)


class LLMResult(_Frozen):
    text: str
    usage: LLMUsage
    truncated: bool
