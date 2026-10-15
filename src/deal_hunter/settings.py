"""Load and validate the user's deal-hunter.toml.

The settings file holds only non-secret preferences (location, budget, which
notifier to use). Credentials never live here: the rendered config refers to
them as ${ENV_VAR} placeholders that ai-marketplace-monitor expands at runtime.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


class SettingsError(ValueError):
    """Raised when deal-hunter.toml is missing a value or has an invalid one."""


AI_PROVIDERS = ("anthropic", "openai", "ollama", "none")
DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "ollama": "qwen2.5:14b",
}

# Notifier -> {aimm user-section field: env var holding its value}.
# Non-secret values (ntfy server, email address, SMTP host) come from settings.
NOTIFIERS: dict[str, dict[str, str]] = {
    "ntfy": {"ntfy_topic": "NTFY_TOPIC"},
    "pushover": {
        "pushover_user_key": "PUSHOVER_USER_KEY",
        "pushover_api_token": "PUSHOVER_API_TOKEN",
    },
    "telegram": {"telegram_token": "TELEGRAM_BOT_TOKEN", "telegram_chat_id": "TELEGRAM_CHAT_ID"},
    "pushbullet": {"pushbullet_token": "PUSHBULLET_TOKEN"},
    "email": {"smtp_password": "SMTP_PASSWORD"},
}

# Notifier -> {aimm field: settings key} for plain, non-secret values that
# upstream requires or cannot always guess. Defaults live in parse_settings.
NOTIFIER_SETTINGS: dict[str, list[str]] = {
    "ntfy": ["ntfy_server"],
    "email": ["email", "smtp_server", "smtp_username", "smtp_port"],
}

DEFAULT_SESSION_FILE = Path("~/.ai-marketplace-monitor/facebook-session.json")

# Facebook marketplace location slugs are lowercase words or numeric page ids.
_CITY_RE = re.compile(r"^[a-z0-9]+$")
_INTERVAL_RE = re.compile(r"^\d+\s*[smhd](\s+\d+\s*[smhd])*$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://[^\s]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


@dataclass(frozen=True)
class LocationSettings:
    search_city: str
    radius: int
    seller_locations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchSettings:
    min_price: int
    max_price: int
    interval: str
    max_interval: str
    include_tiny_pcs: bool
    allow_barebones: bool
    preferences: str
    extra_antikeywords: list[str]


@dataclass(frozen=True)
class AISettings:
    provider: str
    model: str | None
    rating: int
    base_url: str | None


@dataclass(frozen=True)
class NotifySettings:
    methods: list[str]
    # Plain (non-secret) notifier fields rendered onto the aimm user section.
    fields: dict[str, str | int] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    location: LocationSettings
    search: SearchSettings
    ai: AISettings
    notify: NotifySettings
    anonymous: bool
    # Saved Facebook login (`deal-hunter login`). It's a full login to the
    # account, so it's written owner-only and never logged.
    session_file: Path
    # true = never fall back to a login form or the 60-second wait: exit with
    # an error if the saved session is missing or expired. For unattended runs.
    require_session: bool
    # Chromium's sandbox. Only turn off where it can't start (some containers).
    browser_sandbox: bool


def load_settings(path: Path) -> Settings:
    if not path.is_file():
        raise SettingsError(f"{path} not found. Run `deal-hunter init` to create it.")
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{path} is not valid TOML: {e}") from e
    return parse_settings(data)


def parse_settings(data: dict[str, Any]) -> Settings:
    loc = _table(data, "location")
    search = _table(data, "search")
    ai = _table(data, "ai")
    notify = _table(data, "notify")
    fb = _table(data, "facebook")

    search_city = _str(loc, "search_city", "location")
    if search_city == "CHANGE_ME" or not _CITY_RE.match(search_city):
        raise SettingsError(
            "location.search_city must be the Facebook Marketplace location slug or id "
            "(the XXX in facebook.com/marketplace/XXX/search) — lowercase letters/digits only."
        )
    radius = _int(loc, "radius", "location", default=25, lo=1, hi=500)
    seller_locations = _str_list(loc, "seller_locations", "location")

    min_price = _int(search, "min_price", "search", default=40, lo=0, hi=100_000)
    max_price = _int(search, "max_price", "search", default=400, lo=1, hi=100_000)
    if min_price >= max_price:
        raise SettingsError("search.min_price must be less than search.max_price.")
    interval = _interval(search, "interval", default="1h")
    max_interval = _interval(search, "max_interval", default="2h")

    provider = _str(ai, "provider", "ai", default="anthropic").lower()
    if provider not in AI_PROVIDERS:
        raise SettingsError(f"ai.provider must be one of {', '.join(AI_PROVIDERS)}.")
    model = _str(ai, "model", "ai", default=DEFAULT_MODELS.get(provider, "")) or None
    base_url = _str(ai, "base_url", "ai", default="") or None
    if provider == "ollama":
        base_url = base_url or "http://localhost:11434/v1"
    if base_url is not None and not _safe_base_url(base_url):
        # The API key is sent to base_url, so plain http only to this machine.
        raise SettingsError("ai.base_url must be https://, or http:// to localhost.")

    methods = _str_list(notify, "methods", "notify")
    unknown = [m for m in methods if m not in NOTIFIERS]
    if unknown:
        raise SettingsError(
            f"notify.methods has unknown {unknown}; choose from {', '.join(NOTIFIERS)}."
        )
    fields: dict[str, str | int] = {}
    if "ntfy" in methods:
        fields["ntfy_server"] = _str(notify, "ntfy_server", "notify", default="https://ntfy.sh")
        # The topic acts as a bearer secret, so no plain http off this machine.
        if not _safe_base_url(str(fields["ntfy_server"])):
            raise SettingsError("notify.ntfy_server must be https://, or http:// to localhost.")
    if "email" in methods:
        email = _str(notify, "email", "notify", default="")
        if not _EMAIL_RE.match(email):
            raise SettingsError("notify.email must be a valid address when 'email' is a method.")
        fields["email"] = email
        # Upstream guesses smtp.<domain>, the address and 587 when these are unset.
        for key in ("smtp_server", "smtp_username"):
            value = _str(notify, key, "notify", default="")
            if value:
                if key == "smtp_server" and not _HOST_RE.match(value):
                    raise SettingsError("notify.smtp_server must be a hostname.")
                fields[key] = value
        if "smtp_port" in notify:
            fields["smtp_port"] = _int(notify, "smtp_port", "notify", default=587, lo=1, hi=65535)

    anonymous = _bool(fb, "anonymous", "facebook", default=False)
    require_session = _bool(fb, "require_session", "facebook", default=False)
    if anonymous and require_session:
        raise SettingsError("facebook.anonymous and facebook.require_session can't both be true.")

    session_file = Path(
        _str(fb, "session_file", "facebook", default=str(DEFAULT_SESSION_FILE))
    ).expanduser()
    if not session_file.is_absolute():
        # Relative would mean a different file depending on where you run it.
        raise SettingsError("facebook.session_file must be an absolute path or start with ~/.")

    return Settings(
        location=LocationSettings(search_city, radius, seller_locations),
        search=SearchSettings(
            min_price=min_price,
            max_price=max_price,
            interval=interval,
            max_interval=max_interval,
            include_tiny_pcs=_bool(search, "include_tiny_pcs", "search", default=False),
            allow_barebones=_bool(search, "allow_barebones", "search", default=True),
            preferences=_str(search, "preferences", "search", default=""),
            extra_antikeywords=_antikeywords(search),
        ),
        ai=AISettings(
            provider=provider,
            model=model,
            rating=_int(ai, "rating", "ai", default=3, lo=1, hi=5),
            base_url=base_url,
        ),
        notify=NotifySettings(methods=methods, fields=fields),
        anonymous=anonymous,
        session_file=session_file,
        require_session=require_session,
        browser_sandbox=_bool(fb, "browser_sandbox", "facebook", default=True),
    )


def _antikeywords(search: dict[str, Any]) -> list[str]:
    values = _str_list(search, "extra_antikeywords", "search")
    # Rendered as quoted literals; an empty phrase would match every listing.
    if any(not v.replace('"', "").strip() for v in values):
        raise SettingsError("search.extra_antikeywords entries must not be empty.")
    return values


def _safe_base_url(url: str) -> bool:
    if not _URL_RE.match(url):
        return False
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.hostname in ("localhost", "127.0.0.1", "::1")


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise SettingsError(f"[{key}] must be a table.")
    return value


def _str(t: dict[str, Any], key: str, section: str, default: str | None = None) -> str:
    if key not in t:
        if default is None:
            raise SettingsError(f"{section}.{key} is required.")
        return default
    value = t[key]
    if not isinstance(value, str):
        raise SettingsError(f"{section}.{key} must be a string.")
    if "${" in value:
        # Settings are rendered into the aimm config, which expands ${VAR}.
        # Secrets belong in env vars that deal-hunter wires up itself.
        raise SettingsError(f"{section}.{key} must not contain '${{'.")
    return value.strip()


def _str_list(t: dict[str, Any], key: str, section: str) -> list[str]:
    value = t.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SettingsError(f"{section}.{key} must be a string or list of strings.")
    if any("${" in v for v in value):
        raise SettingsError(f"{section}.{key} must not contain '${{'.")
    return [v.strip() for v in value if v.strip()]


def _int(t: dict[str, Any], key: str, section: str, *, default: int, lo: int, hi: int) -> int:
    value = t.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise SettingsError(f"{section}.{key} must be an integer between {lo} and {hi}.")
    return value


def _bool(t: dict[str, Any], key: str, section: str, *, default: bool) -> bool:
    value = t.get(key, default)
    if not isinstance(value, bool):
        raise SettingsError(f"{section}.{key} must be true or false.")
    return value


def _interval(t: dict[str, Any], key: str, *, default: str) -> str:
    value = _str(t, key, "search", default=default)
    if not _INTERVAL_RE.match(value):
        raise SettingsError(f"search.{key} must look like '30m', '1h' or '1h 30m'.")
    return value
