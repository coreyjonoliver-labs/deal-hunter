from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deal_hunter.settings import SettingsError, load_settings, parse_settings


def test_defaults_fill_in(base_settings: dict[str, Any]) -> None:
    s = parse_settings(base_settings)
    assert s.location.search_city == "seattle"
    assert s.ai.model == "claude-haiku-4-5-20251001"
    assert s.ai.rating == 3
    assert s.search.interval == "1h"
    assert s.anonymous is False
    assert s.session_file == Path.home() / ".ai-marketplace-monitor/facebook-session.json"
    assert s.require_session is False
    assert s.browser_sandbox is True


def test_session_settings(base_settings: dict[str, Any]) -> None:
    base_settings["facebook"] = {"session_file": "~/fb.json", "require_session": True}
    s = parse_settings(base_settings)
    assert s.session_file == Path.home() / "fb.json"
    assert s.require_session is True


def test_example_settings_require_a_real_city() -> None:
    example = Path(__file__).parents[1] / "src/deal_hunter/example-settings.toml"
    with pytest.raises(SettingsError, match="search_city"):
        load_settings(example)


def test_missing_file_points_at_init(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="deal-hunter init"):
        load_settings(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    ("section", "key", "value", "match"),
    [
        ("location", "search_city", "New York", "search_city"),
        ("location", "radius", 0, "radius"),
        ("location", "radius", True, "radius"),
        ("search", "min_price", 500, "less than"),
        ("search", "interval", "soon", "interval"),
        ("ai", "provider", "skynet", "provider"),
        ("ai", "rating", 6, "rating"),
        ("notify", "methods", ["carrier-pigeon"], "unknown"),
        ("notify", "methods", ["email"], "notify.email"),
        ("search", "preferences", "${ANTHROPIC_API_KEY}", r"\$\{"),
        ("location", "seller_locations", ["${HOME}"], r"\$\{"),
        ("facebook", "anonymous", "yes", "true or false"),
        ("facebook", "require_session", 1, "true or false"),
        ("facebook", "session_file", "${HOME}/x", r"\$\{"),
        ("facebook", "session_file", 3, "string"),
        ("facebook", "session_file", "fb.json", "absolute"),
        ("facebook", "browser_sandbox", "no", "true or false"),
        ("notify", "ntfy_server", "ftp://x", "ntfy_server"),
        ("ai", "base_url", "http://evil.example/v1", "base_url"),
        ("ai", "base_url", "not a url", "base_url"),
        ("notify", "ntfy_server", "http://ntfy.example", "ntfy_server"),
        ("search", "extra_antikeywords", ['""'], "must not be empty"),
    ],
)
def test_invalid_values_are_rejected(
    base_settings: dict[str, Any], section: str, key: str, value: Any, match: str
) -> None:
    base_settings.setdefault(section, {})[key] = value
    with pytest.raises(SettingsError, match=match):
        parse_settings(base_settings)


def test_ollama_gets_local_default_url(base_settings: dict[str, Any]) -> None:
    base_settings["ai"] = {"provider": "ollama"}
    s = parse_settings(base_settings)
    assert s.ai.base_url == "http://localhost:11434/v1"


def test_invalid_toml(tmp_path: Path) -> None:
    p = tmp_path / "s.toml"
    p.write_text("[location\n")
    with pytest.raises(SettingsError, match="not valid TOML"):
        load_settings(p)


def test_https_or_loopback_base_url_allowed(base_settings: dict[str, Any]) -> None:
    base_settings["ai"] = {"provider": "openai", "base_url": "https://proxy.example/v1"}
    assert parse_settings(base_settings).ai.base_url == "https://proxy.example/v1"
    base_settings["ai"] = {"provider": "ollama", "base_url": "http://127.0.0.1:11434/v1"}
    assert parse_settings(base_settings).ai.base_url == "http://127.0.0.1:11434/v1"


def test_anonymous_conflicts_with_require_session(base_settings: dict[str, Any]) -> None:
    base_settings["facebook"] = {"anonymous": True, "require_session": True}
    with pytest.raises(SettingsError, match="both"):
        parse_settings(base_settings)
