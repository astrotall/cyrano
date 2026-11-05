from __future__ import annotations

import asyncio

import pytest

from tests.capped_loop import CappedLoop

SMALL_CAP = 5
EVEN_CAP = 4


async def _never(_: float) -> bool:
    return False


async def _forever(driver: CappedLoop) -> None:
    while True:
        await driver.sleep(1)


async def test_the_loop_stops_when_the_outcome_is_reached_and_not_before() -> None:
    seen: list[float] = []

    async def third_call(seconds: float) -> bool:
        seen.append(seconds)
        return len(seen) == 3

    driver = CappedLoop(third_call)

    await driver.run(_forever(driver))

    assert seen == [1, 1, 1]


async def test_the_cap_fails_with_a_clear_assertion_error() -> None:
    driver = CappedLoop(_never, cap=SMALL_CAP)

    with pytest.raises(AssertionError, match=f"{SMALL_CAP} sleeps"):
        await driver.run(_forever(driver))


async def test_the_exact_cap_is_still_allowed() -> None:
    seen: list[float] = []

    async def at_the_cap(seconds: float) -> bool:
        seen.append(seconds)
        return len(seen) == SMALL_CAP

    driver = CappedLoop(at_the_cap, cap=SMALL_CAP)

    await driver.run(_forever(driver))

    assert len(seen) == SMALL_CAP


async def test_the_cap_still_fails_when_the_loop_swallows_the_first_failure() -> None:
    driver = CappedLoop(_never, cap=EVEN_CAP)
    swallowed: list[AssertionError] = []

    async def swallows_one_sleep_of_two() -> None:
        while True:
            try:
                await driver.sleep(0)
            except AssertionError as exc:
                swallowed.append(exc)
            await driver.sleep(1)

    with pytest.raises(AssertionError, match=f"{EVEN_CAP} sleeps"):
        await driver.run(swallows_one_sleep_of_two())

    assert len(swallowed) == 1


async def test_a_loop_that_ends_before_the_outcome_fails() -> None:
    async def ends_at_once() -> None:
        return None

    with pytest.raises(AssertionError, match="ended before"):
        await CappedLoop(_never).run(ends_at_once())


async def test_a_failure_of_the_loop_itself_is_not_hidden() -> None:
    async def breaks() -> None:
        raise RuntimeError("fake failure")

    with pytest.raises(RuntimeError, match="fake failure"):
        await CappedLoop(_never).run(breaks())


async def test_no_task_is_left_behind() -> None:
    async def at_once(_: float) -> bool:
        return True

    before = asyncio.all_tasks()
    driver = CappedLoop(at_once)

    await driver.run(_forever(driver))

    assert asyncio.all_tasks() == before
