from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deal_hunter.settings import SettingsError, load_settings, parse_settings, tomllib


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


def _with_watches(base: dict[str, Any], *watches: dict[str, Any]) -> dict[str, Any]:
    base = {**base, "search": {"interval": "30m", "max_interval": "1h"}}
    base["watch"] = list(watches)
    return base


DESK = {"name": "standing desk", "search_phrases": ["standing desk"], "max_price": 300}


def test_legacy_search_becomes_mini_pc_watches(base_settings: dict[str, Any]) -> None:
    base_settings["search"] |= {"include_tiny_pcs": True, "extra_antikeywords": ["celeron"]}
    nuc, tiny = parse_settings(base_settings).watches
    assert (nuc.name, nuc.preset, tiny.name, tiny.preset) == (
        "nuc",
        "mini-pc",
        "tiny_pc",
        "tiny-pc",
    )
    assert nuc.min_price == 40 and nuc.max_price == 400 and nuc.rating == 3
    assert nuc.antikeywords == ["celeron"] and '"mini pc"' in nuc.keywords


def test_generic_watch(base_settings: dict[str, Any]) -> None:
    w = {**DESK, "keywords": ["desk", 'sit "stand"'], "antikeywords": ["kids"], "rating": 4}
    (watch,) = parse_settings(_with_watches(base_settings, w)).watches
    assert watch.preset is None and watch.search_phrases == ["standing desk"]
    assert watch.keywords == ['"desk"', '"sit stand"']  # literals, never expressions
    assert watch.antikeywords == ["kids"] and watch.min_price == 0 and watch.rating == 4


def test_generic_watch_has_no_keyword_gate_by_default(base_settings: dict[str, Any]) -> None:
    (watch,) = parse_settings(_with_watches(base_settings, DESK)).watches
    assert watch.keywords == []


def test_preset_fills_in_and_can_be_overridden(base_settings: dict[str, Any]) -> None:
    mini = {"name": "nuc", "preset": "mini-pc", "max_price": 400, "allow_barebones": False}
    custom = {"name": "nuc2", "preset": "mini-pc", "max_price": 400, "search_phrases": ["nuc 13"]}
    a, b = parse_settings(_with_watches(base_settings, mini, custom)).watches
    assert a.search_phrases == ["nuc", "beelink", "minisforum", "mini pc"]
    assert not a.allow_barebones
    assert b.search_phrases == ["nuc 13"] and b.keywords == a.keywords


@pytest.mark.parametrize(
    ("watch", "message"),
    [
        ({"name": "x", "max_price": 10}, "search_phrases is required"),
        ({"name": "x", "search_phrases": ["x"]}, "max_price is required"),
        ({**DESK, "min_price": 300}, "min_price must be less"),
        ({**DESK, "maxprice": 3}, "unknown maxprice"),
        ({**DESK, "preset": "boats"}, "preset must be one of"),
        ({**DESK, "allow_barebones": False}, "allow_barebones only applies"),
        ({**DESK, "name": "bad/name"}, "watch.name"),
        ({**DESK, "name": ""}, "watch.name"),
        ({**DESK, "keywords": ['""']}, "must not be empty"),
        ({**DESK, "search_phrases": [f"p{i}" for i in range(11)]}, "at most 10"),
        ({**DESK, "preferences": "${ANTHROPIC_API_KEY}"}, "must not contain"),
    ],
)
def test_bad_watches(base_settings: dict[str, Any], watch: dict[str, Any], message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        parse_settings(_with_watches(base_settings, watch))


def test_watch_names_are_unique(base_settings: dict[str, Any]) -> None:
    with pytest.raises(SettingsError, match="different name"):
        parse_settings(_with_watches(base_settings, DESK, {**DESK, "name": "Standing Desk"}))


def test_legacy_keys_next_to_watches_are_an_error(base_settings: dict[str, Any]) -> None:
    data = _with_watches(base_settings, DESK)
    data["search"]["max_price"] = 400
    with pytest.raises(SettingsError, match=r"search\.max_price belong in a \[\[watch\]\]"):
        parse_settings(data)


def test_watch_must_be_tables(base_settings: dict[str, Any]) -> None:
    data = _with_watches(base_settings)
    with pytest.raises(SettingsError, match="between 1 and"):
        parse_settings(data)
    data["watch"] = {"name": "x"}
    with pytest.raises(SettingsError, match="list of"):
        parse_settings(data)


def test_example_settings_parse_once_the_city_is_set() -> None:
    example = Path(__file__).parents[1] / "src/deal_hunter/example-settings.toml"
    text = example.read_text().replace('"CHANGE_ME"', '"seattle"')
    data = tomllib.loads(text)
    (watch,) = parse_settings(data).watches
    assert watch.preset == "mini-pc" and watch.max_price == 400
    head, sep, tail = text.partition("# [[watch]]")
    block, ai, rest = tail.partition("[ai]")
    uncommented = head + sep[2:] + block.replace("\n# ", "\n") + ai + rest
    assert [w.name for w in parse_settings(tomllib.loads(uncommented)).watches][-1] == (
        "standing desk"
    )


def test_shipped_settings_hold_no_location() -> None:
    # Committed for others to copy, so filling one in place would publish
    # the author's area.
    root = Path(__file__).parents[1]
    for path in (
        root / "src/deal_hunter/example-settings.toml",
        root / "deploy/kubernetes/example/deal-hunter.toml",
    ):
        data = tomllib.loads(path.read_text())
        assert data["location"]["search_city"] == "CHANGE_ME", path
        assert "seller_locations" not in data["location"], path


@pytest.mark.parametrize(
    ("notify", "message"),
    [
        ({"smtp_username": "x@evil.example@gmail.com"}, "more than one '@'"),
        ({"smtp_username": "apikey"}, "smtp_server is required"),
    ],
)
def test_smtp_username_must_name_one_host(
    base_settings: dict[str, Any], notify: dict[str, str], message: str
) -> None:
    base_settings["notify"] = {"methods": ["email"], "email": "me@example.org", **notify}
    with pytest.raises(SettingsError, match=message):
        parse_settings(base_settings)
    base_settings["notify"]["smtp_server"] = "smtp.example.org"
    if "@" not in notify["smtp_username"]:
        assert parse_settings(base_settings).notify.fields["smtp_username"] == "apikey"


def test_discord_is_a_known_method_without_extra_settings(base_settings: dict[str, Any]) -> None:
    base_settings["notify"] = {"methods": ["discord", "ntfy"]}
    settings = parse_settings(base_settings)
    assert settings.notify.methods == ["discord", "ntfy"]
    assert "ntfy_server" in settings.notify.fields
    assert not any("discord" in key for key in settings.notify.fields)


def test_the_webhook_url_cannot_come_from_the_settings_file(base_settings: dict[str, Any]) -> None:
    # Unknown notify keys are ignored today; make sure none is ever carried through.
    base_settings["notify"] = {
        "methods": ["discord"],
        "discord_webhook_url": "https://discord.com/api/webhooks/1/abc",
    }
    assert "discord_webhook_url" not in parse_settings(base_settings).notify.fields


def test_unknown_method_message_lists_discord(base_settings: dict[str, Any]) -> None:
    base_settings["notify"] = {"methods": ["carrier-pigeon"]}
    with pytest.raises(SettingsError, match="discord"):
        parse_settings(base_settings)
