"""The rendered config must load cleanly in ai-marketplace-monitor's own Config."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from ai_marketplace_monitor.config import Config

from deal_hunter.render import render, required_env
from deal_hunter.settings import NOTIFIERS, parse_settings

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def _load(tmp_path: Path, data: dict[str, Any]) -> Config:  # type: ignore[type-arg]
    path = tmp_path / "aimm.toml"
    path.write_text(render(parse_settings(data)))
    return Config([path])


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("NTFY_TOPIC", "topic")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "u")
    monkeypatch.setenv("PUSHOVER_API_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("PUSHBULLET_TOKEN", "p")


def test_default_config_loads(tmp_path: Path, base_settings: dict[str, Any]) -> None:
    cfg = _load(tmp_path, base_settings)
    item = cfg.item["nuc"]
    assert item.search_phrases == ["nuc", "beelink", "minisforum", "mini pc"]
    assert item.min_price == "40"
    assert cfg.marketplace["facebook"].search_city == ["seattle"]
    assert cfg.ai["anthropic"].api_key == "sk-test"
    assert cfg.user["me"].ntfy_topic == "topic"  # type: ignore[attr-defined]
    assert "tiny_pc" not in cfg.item
    assert cfg.marketplace["facebook"].delivery_method == ["local_pick_up"]


def test_secrets_never_rendered_literally(base_settings: dict[str, Any]) -> None:
    text = render(parse_settings(base_settings))
    assert "${ANTHROPIC_API_KEY}" in text
    assert "${NTFY_TOPIC}" in text
    assert "sk-test" not in text


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
def test_other_ai_providers_load(
    tmp_path: Path, base_settings: dict[str, Any], provider: str
) -> None:
    base_settings["ai"] = {"provider": provider}
    cfg = _load(tmp_path, base_settings)
    if provider == "none":
        assert cfg.ai == {}
        assert cfg.item["nuc"].ai == []
    else:
        assert provider in cfg.ai


def test_tiny_pcs_and_barebones_options(tmp_path: Path, base_settings: dict[str, Any]) -> None:
    base_settings["search"].update(
        include_tiny_pcs=True, allow_barebones=False, extra_antikeywords=["celeron j4125"]
    )
    cfg = _load(tmp_path, base_settings)
    assert "tiny_pc" in cfg.item
    anti = cfg.item["nuc"].antikeywords
    assert "barebone" in anti
    assert '"celeron j4125"' in anti


def test_console_only_and_email(tmp_path: Path, base_settings: dict[str, Any]) -> None:
    base_settings["notify"] = {"methods": []}
    cfg = _load(tmp_path, base_settings)
    assert "me" in cfg.user
    assert "ntfy_topic" not in render(parse_settings(base_settings))

    base_settings["notify"] = {
        "methods": ["email"],
        "email": "me@example.com",
        "smtp_server": "smtp.fastmail.com",
        "smtp_port": 465,
    }
    cfg = _load(tmp_path, base_settings)
    user = cfg.user["me"]
    assert user.email == ["me@example.com"]  # type: ignore[attr-defined]
    assert user.smtp_server == "smtp.fastmail.com"  # type: ignore[attr-defined]
    assert user.smtp_port == 465  # type: ignore[attr-defined]


@pytest.mark.parametrize("method", list(NOTIFIERS))
def test_every_notifier_has_upstream_required_fields(
    tmp_path: Path, base_settings: dict[str, Any], method: str
) -> None:
    # Loading is not enough: aimm silently skips a notifier whose
    # required_fields are unset, so check them against upstream classes.
    from ai_marketplace_monitor.notification import NotificationConfig

    base_settings["notify"] = {"methods": [method], "email": "me@example.com"}
    user = _load(tmp_path, base_settings).user["me"]
    notifier_classes = [
        c
        for c in NotificationConfig.__subclasses__()
        + [g for c in NotificationConfig.__subclasses__() for g in c.__subclasses__()]
        if c.notify_method == method
    ]
    assert notifier_classes, method
    for cls in notifier_classes:
        for name in cls.required_fields:
            assert getattr(user, name, None) is not None, f"{method}: {name} unset"


def test_ntfy_defaults_to_public_server(tmp_path: Path, base_settings: dict[str, Any]) -> None:
    assert _load(tmp_path, base_settings).user["me"].ntfy_server == "https://ntfy.sh"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("raw", "rendered"),
    [
        ("celeron j4125", '"celeron j4125"'),
        ('"a" AND "b"', '"a AND b"'),
        ('it\'s "mint"', '"it\'s mint"'),
    ],
)
def test_extra_antikeywords_are_always_literal(
    base_settings: dict[str, Any], raw: str, rendered: str
) -> None:
    base_settings["search"]["extra_antikeywords"] = [raw]
    data = tomllib.loads(render(parse_settings(base_settings)))
    assert data["item"]["nuc"]["antikeywords"][-1] == rendered


def test_hostile_strings_stay_inside_their_values(base_settings: dict[str, Any]) -> None:
    evil = 'x"""\n[item.pwn]\nsearch_phrases = "free stuff"\n'
    base_settings["search"]["preferences"] = evil
    base_settings["location"]["seller_locations"] = ['a"\n[ai.evil]']
    data = tomllib.loads(render(parse_settings(base_settings)))
    assert set(data["item"]) == {"nuc"}
    assert set(data["ai"]) == {"anthropic"}
    assert data["item"]["nuc"]["description"] == evil.strip()


def test_required_env(base_settings: dict[str, Any]) -> None:
    base_settings["notify"]["methods"] = ["ntfy", "pushover"]
    assert required_env(parse_settings(base_settings)) == [
        "ANTHROPIC_API_KEY",
        "NTFY_TOPIC",
        "PUSHOVER_USER_KEY",
        "PUSHOVER_API_TOKEN",
    ]


def test_watches_render_as_items(tmp_path: Path, base_settings: dict[str, Any]) -> None:
    base_settings["search"] = {}
    base_settings["watch"] = [
        {"name": "nuc", "preset": "mini-pc", "max_price": 400, "allow_barebones": False},
        {
            "name": "standing desk",
            "search_phrases": ["standing desk", "sit stand desk"],
            "antikeywords": ["kids"],
            "max_price": 300,
            "rating": 4,
            "preferences": "Electric, dual motor.",
        },
    ]
    cfg = _load(tmp_path, base_settings)
    nuc, desk = cfg.item["nuc"], cfg.item["standing desk"]
    assert '"no ram"' in nuc.antikeywords and '"bios locked"' in nuc.antikeywords
    assert "Intel NUC" in (nuc.prompt or "")
    assert desk.keywords is None  # no gate: the AI judges everything found
    assert desk.search_phrases == ["standing desk", "sit stand desk"]
    assert '"kids"' in desk.antikeywords and '"for parts"' in desk.antikeywords
    assert '"bios locked"' not in desk.antikeywords
    assert desk.rating == [4] and desk.description == "Electric, dual motor."
    assert "mini PC" not in (desk.prompt or "") and "untrusted data" in (desk.prompt or "")
