from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger("cyrano.health")

HEARTBEAT_ENV = "HEARTBEAT_PATH"
HEARTBEAT_INTERVAL = 10.0
DEFAULT_MAX_AGE = 60.0
HEALTHCHECK_DESCRIPTION = "Heartbeat file and the container healthcheck entry point."


def default_heartbeat_path() -> Path:
    return Path(tempfile.gettempdir()) / "cyrano-heartbeat"


def touch(path: Path) -> None:
    path.touch()


def is_alive(path: Path, max_age: float = DEFAULT_MAX_AGE, *, now: float | None = None) -> bool:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (time.time() if now is None else now) - mtime <= max_age


async def heartbeat_loop(path: Path, interval: float = HEARTBEAT_INTERVAL) -> None:
    while True:
        try:
            touch(path)
        except OSError:
            logger.warning("cannot write heartbeat file", extra={"path": str(path)}, exc_info=True)
        await asyncio.sleep(interval)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cyrano.health", description=HEALTHCHECK_DESCRIPTION
    )
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE, help="seconds")
    parser.add_argument("--path", type=Path, default=None, help=f"default: ${HEARTBEAT_ENV}")
    args = parser.parse_args(argv)

    path: Path = args.path or Path(os.environ.get(HEARTBEAT_ENV) or default_heartbeat_path())
    if is_alive(path, args.max_age):
        return 0
    print(f"unhealthy: {path} missing or older than {args.max_age:g}s", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
