from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cyrano.config import load_settings
from cyrano.logging import configure_logging
from cyrano.main import run
from tests.conftest import (
    OPTIONAL_ENV,
    PERSONAL_SETTING_NAMES,
    PERSONAL_VALUES,
    REQUIRED_ENV,
    SECRET_VALUES,
)


async def test_run_logs_startup_and_stops_on_event(
    full_env: dict[str, str], heartbeat_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = load_settings(env_file=None)
    configure_logging("INFO")
    stop = asyncio.Event()

    task = asyncio.create_task(run(settings, stop))
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line]
    assert [r["msg"] for r in records] == [
        "cyrano started",
        "cyrano shutting down",
        "cyrano stopped",
    ]
    for secret in (*SECRET_VALUES, *PERSONAL_VALUES):
        assert secret not in out
    for name in PERSONAL_SETTING_NAMES:
        assert name not in records[0]


def _spawn(env: dict[str, str]) -> subprocess.Popen[str]:
    full = {**os.environ, **env}
    return subprocess.Popen(
        [sys.executable, "-m", "cyrano"],
        env=full,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _healthcheck(path: Path) -> int:
    cmd = [sys.executable, "-m", "cyrano.health", "--path", str(path)]
    return subprocess.run(cmd, capture_output=True, check=False).returncode  # noqa: S603


def test_process_exits_cleanly_on_sigterm(full_env: dict[str, str], heartbeat_file: Path) -> None:
    proc = _spawn({**full_env, "HEARTBEAT_PATH": str(heartbeat_file)})
    assert proc.stdout is not None
    first = proc.stdout.readline()
    assert json.loads(first)["msg"] == "cyrano started"
    for value in PERSONAL_VALUES:
        assert value not in first
    for _ in range(50):
        if _healthcheck(heartbeat_file) == 0:
            break
        time.sleep(0.1)
    else:
        pytest.fail("healthcheck never reported the running app as alive")

    proc.send_signal(signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=10)

    assert proc.returncode == 0
    assert "cyrano stopped" in stdout
    assert not heartbeat_file.exists()
    assert _healthcheck(heartbeat_file) == 1


def test_process_fails_fast_without_config() -> None:
    env = {k: v for k, v in os.environ.items() if k not in {*REQUIRED_ENV, *OPTIONAL_ENV}}
    proc = subprocess.run(
        [sys.executable, "-m", "cyrano"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        cwd="/",
        check=False,
    )

    assert proc.returncode == 2
    assert "Missing required environment variables" in proc.stderr
    assert "BOT_TOKEN" in proc.stderr
    assert "Traceback" not in proc.stderr


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


def _exists(path: Path) -> bool:
    return path.exists()


async def test_run_keeps_touching_heartbeat_and_removes_it_on_stop(
    full_env: dict[str, str], heartbeat_file: Path
) -> None:
    settings = load_settings(env_file=None)
    stop = asyncio.Event()

    task = asyncio.create_task(run(settings, stop, heartbeat_interval=0.02))
    await asyncio.sleep(0.1)
    first = _mtime_ns(heartbeat_file)
    await asyncio.sleep(0.1)
    assert _mtime_ns(heartbeat_file) > first
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert not _exists(heartbeat_file)
