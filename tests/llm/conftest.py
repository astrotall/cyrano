from __future__ import annotations

import pytest

from cyrano.config import Settings, load_settings
from tests.llm.fakes import FakeSleep


@pytest.fixture
def settings(full_env: dict[str, str]) -> Settings:
    return load_settings(env_file=None)


@pytest.fixture
def sleep() -> FakeSleep:
    return FakeSleep()
