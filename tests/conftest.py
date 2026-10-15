from __future__ import annotations

import signal
from collections.abc import Iterator
from typing import Any

import pytest

from deal_hunter import session


@pytest.fixture(autouse=True)
def no_real_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep tests away from the real ~/.ai-marketplace-monitor and Time Machine."""
    monkeypatch.setattr(session, "AIMM_HOME", tmp_path_factory.mktemp("aimm-home"))
    monkeypatch.setattr(session, "exclude_from_backups", lambda path: True)
    for name in ("DEAL_HUNTER_AWAITING_SEED", "DEAL_HUNTER_RESTARTS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def restore_sigterm() -> Iterator[None]:
    """cli.main installs a SIGTERM handler; don't let it outlive the test."""
    saved = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, saved)


@pytest.fixture
def base_settings() -> dict[str, Any]:
    return {
        "location": {"search_city": "seattle", "radius": 30},
        "search": {"min_price": 40, "max_price": 400},
        "ai": {"provider": "anthropic"},
        "notify": {"methods": ["ntfy"]},
    }
