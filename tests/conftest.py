from __future__ import annotations

import os
import signal
import tempfile
from collections.abc import Iterator
from typing import Any

# Before anything imports aimm: its utils module creates and opens
# ~/.ai-marketplace-monitor/cache.db at import, and the tests must never touch
# the real one (nor can they, inside a sandbox).
os.environ["HOME"] = tempfile.mkdtemp(prefix="deal-hunter-test-home-")

import pytest  # noqa: E402

from deal_hunter import session  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep tests away from the real ~/.ai-marketplace-monitor and Time Machine."""
    monkeypatch.setattr(session, "AIMM_HOME", tmp_path_factory.mktemp("aimm-home"))
    monkeypatch.setattr(session, "exclude_from_backups", lambda path: True)
    for name in ("DEAL_HUNTER_AWAITING_SEED", "DEAL_HUNTER_RESTARTS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_otel_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A developer's own OTEL_* variables must not turn tracing on in a test."""
    for name in [n for n in os.environ if n.startswith("OTEL_")]:
        monkeypatch.delenv(name)
    yield
    # A test that left tracing on would trace (and slow) every test after it.
    from deal_hunter import tracing

    tracing.shutdown(timeout_s=1)
    tracing._unhook_logging()  # production keeps the hook for the life of the process


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
