from __future__ import annotations

from cyrano.domain.enums import Role
from cyrano.domain.llm import LLMResult, LLMUsage
from cyrano.services.llm_check import CHECK_MAX_TOKENS, check_llm
from cyrano.services.llm_client import LLMClient
from tests.services.fakes import FakeLLMClient


def _result(cache_hit_tokens: int | None) -> LLMResult:
    return LLMResult(
        text="OK",
        usage=LLMUsage(input_tokens=9, output_tokens=1, cache_hit_tokens=cache_hit_tokens),
        truncated=False,
    )


async def test_the_check_sends_one_tiny_request_and_reports_usage_and_latency() -> None:
    client = FakeLLMClient(result=_result(4))
    ticks = iter([5.0, 5.5])

    report = await check_llm(client, lambda: next(ticks))

    assert (report.input_tokens, report.output_tokens, report.cache_hit_tokens) == (9, 1, 4)
    assert report.latency_ms == 500
    assert len(client.calls) == 1
    messages, temperature, max_tokens = client.calls[0]
    assert [m.role for m in messages] == [Role.SYSTEM, Role.USER]
    assert max_tokens == CHECK_MAX_TOKENS
    assert temperature is None


def test_the_fake_client_satisfies_the_protocol() -> None:
    client: LLMClient = FakeLLMClient()

    assert client is not None
