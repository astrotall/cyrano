from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from tests.conftest import TOUCHED_LOGGERS

TEMPLATE_PREFIX = "cyrano_tpl_"
CLONE_PREFIX = "cyrano_test_"
WORKER_ENV = "PYTEST_XDIST_WORKER"
MAX_IDENTIFIER_LENGTH = 63
STALE_AFTER_SECONDS = 6 * 60 * 60
TEMPLATE_NAME_PATTERN = re.compile(
    rf"^{TEMPLATE_PREFIX}(?P<epoch>\d{{1,12}})_\d+(?:_[A-Za-z0-9]+)?$"
)
CLONE_NAME_PATTERN = re.compile(rf"^{CLONE_PREFIX}\d{{1,12}}_\d+(?:_[A-Za-z0-9]+)?_\d+$")
UNUSED_POLL_ATTEMPTS = 100
UNUSED_POLL_SECONDS = 0.05

_clone_counter = itertools.count(1)


def _name(prefix: str, epoch: int, pid: int, worker: str | None, serial: int | None) -> str:
    parts = [f"{prefix}{epoch}", str(pid)]
    if worker:
        parts.append(worker)
    if serial is not None:
        parts.append(str(serial))
    name = "_".join(parts)
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"database name longer than {MAX_IDENTIFIER_LENGTH} characters")
    return name


def template_name(epoch: int, pid: int, worker: str | None) -> str:
    return _name(TEMPLATE_PREFIX, epoch, pid, worker, None)


def clone_name(epoch: int, pid: int, worker: str | None) -> str:
    return _name(CLONE_PREFIX, epoch, pid, worker, next(_clone_counter))


def template_epoch(name: str) -> int | None:
    match = TEMPLATE_NAME_PATTERN.match(name)
    return int(match.group("epoch")) if match else None


def stale_templates(names: Iterable[str], now: int) -> list[str]:
    stale = []
    for name in names:
        epoch = template_epoch(name)
        if epoch is not None and now - epoch > STALE_AFTER_SECONDS:
            stale.append(name)
    return stale


@contextmanager
def preserved_loggers() -> Iterator[None]:
    loggers = [logging.getLogger(), *(logging.getLogger(n) for n in TOUCHED_LOGGERS)]
    saved = [(lg, list(lg.handlers), lg.level, lg.propagate) for lg in loggers]
    try:
        yield
    finally:
        for lg, handlers, level, propagate in saved:
            lg.handlers[:] = handlers
            lg.setLevel(level)
            lg.propagate = propagate
