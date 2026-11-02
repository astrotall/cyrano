"""The parts of the provider's response that the client reads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WireMessage(_Wire):
    content: str | None = None


class WireChoice(_Wire):
    message: WireMessage
    finish_reason: str | None = None


class WireUsage(_Wire):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0)


class WireResponse(_Wire):
    choices: list[WireChoice] = Field(min_length=1)
    usage: WireUsage
