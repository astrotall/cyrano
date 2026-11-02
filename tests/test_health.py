from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from cyrano import health


def test_missing_file_is_not_alive(tmp_path: Path) -> None:
    assert not health.is_alive(tmp_path / "nope")


def test_fresh_file_is_alive_and_stale_file_is_not(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    health.touch(beat)
    mtime = beat.stat().st_mtime

    assert health.is_alive(beat, max_age=60, now=mtime + 59)
    assert health.is_alive(beat, max_age=60, now=mtime + 60)
    assert not health.is_alive(beat, max_age=60, now=mtime + 61)


def test_touch_bumps_mtime(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    health.touch(beat)
    os.utime(beat, (1, 1))

    health.touch(beat)

    assert beat.stat().st_mtime > 1_000_000


def test_default_path_is_in_tempdir() -> None:
    assert health.default_heartbeat_path().name == "cyrano-heartbeat"


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    beat = tmp_path / "beat"

    assert health.main(["--path", str(beat)]) == 1
    assert "unhealthy" in capsys.readouterr().err

    health.touch(beat)
    assert health.main(["--path", str(beat)]) == 0

    os.utime(beat, (1, 1))
    assert health.main(["--path", str(beat)]) == 1
    assert health.main(["--path", str(beat), "--max-age", "1e12"]) == 0


def test_main_reads_path_from_env(heartbeat_file: Path) -> None:
    assert health.main([]) == 1

    health.touch(heartbeat_file)

    assert health.main([]) == 0


async def test_heartbeat_loop_touches_repeatedly(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    task = asyncio.create_task(health.heartbeat_loop(beat, interval=0.01))
    try:
        await asyncio.sleep(0.05)
        os.utime(beat, (1, 1))
        await asyncio.sleep(0.05)
        assert health.is_alive(beat, max_age=5)
    finally:
        task.cancel()


async def test_heartbeat_loop_survives_write_errors(tmp_path: Path) -> None:
    beat = tmp_path / "missing-dir" / "beat"
    task = asyncio.create_task(health.heartbeat_loop(beat, interval=0.01))
    await asyncio.sleep(0.05)
    assert not task.done()

    (tmp_path / "missing-dir").mkdir()
    await asyncio.sleep(0.05)
    task.cancel()

    assert beat.exists()
