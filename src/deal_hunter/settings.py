"""Load and validate the user's deal-hunter.toml.

The settings file holds only non-secret preferences (location, what to watch
for and at what price, which notifier to use). Credentials never live here:
the rendered config refers to them as ${ENV_VAR} placeholders that
ai-marketplace-monitor expands at runtime.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from deal_hunter.profile import PRESETS

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
# A watch's name is aimm's item name: the AI reads "A user wants to buy a
# <name>", and it's part of every cached rating's key, so renaming a watch
# rates its listings again.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
MAX_WATCHES = 20
MAX_PHRASES = 10

WATCH_KEYS = {
    "name",
    "preset",
    "search_phrases",
    "keywords",
    "antikeywords",
    "min_price",
    "max_price",
    "preferences",
    "rating",
    "allow_barebones",
}
# [search] keys from before [[watch]] existed: a single mini-PC watch.
LEGACY_SEARCH_KEYS = {
    "min_price",
    "max_price",
    "include_tiny_pcs",
    "allow_barebones",
    "preferences",
    "extra_antikeywords",
}


@dataclass(frozen=True)
class LocationSettings:
    search_city: str
    radius: int
    seller_locations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchSettings:
    interval: str
    max_interval: str


@dataclass(frozen=True)
class WatchSettings:
    """One thing to look for: its searches, filters, budget and how to judge it."""

    name: str
    preset: str | None
    search_phrases: list[str]
    # In aimm's syntax, ready to render. Empty means no keyword gate.
    keywords: list[str]
    # The user's own, as literal phrases (rendered quoted).
    antikeywords: list[str]
    min_price: int
    max_price: int
    preferences: str
    rating: int
    allow_barebones: bool


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
    watches: list[WatchSettings]
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

    interval = _interval(search, "interval", default="1h")
    max_interval = _interval(search, "max_interval", default="2h")

    rating = _int(ai, "rating", "ai", default=3, lo=1, hi=5)
    watches = _watches(data, search, rating)

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
        search=SearchSettings(interval=interval, max_interval=max_interval),
        watches=watches,
        ai=AISettings(
            provider=provider,
            model=model,
            rating=rating,
            base_url=base_url,
        ),
        notify=NotifySettings(methods=methods, fields=fields),
        anonymous=anonymous,
        session_file=session_file,
        require_session=require_session,
        browser_sandbox=_bool(fb, "browser_sandbox", "facebook", default=True),
    )


def _watches(data: dict[str, Any], search: dict[str, Any], rating: int) -> list[WatchSettings]:
    if "watch" not in data:
        return _legacy_watches(search, rating)
    legacy = sorted(LEGACY_SEARCH_KEYS & search.keys())
    if legacy:
        raise SettingsError(
            f"search.{', search.'.join(legacy)} belong in a [[watch]] now; move them there."
        )
    tables = data["watch"]
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise SettingsError("watch must be a list of [[watch]] tables.")
    if not 1 <= len(tables) <= MAX_WATCHES:
        raise SettingsError(f"Add between 1 and {MAX_WATCHES} [[watch]] tables.")
    watches = [_watch(t, rating) for t in tables]
    names = [w.name.lower() for w in watches]
    if len(set(names)) != len(names):
        raise SettingsError("Each [[watch]] needs a different name.")
    return watches


def _watch(t: dict[str, Any], default_rating: int) -> WatchSettings:
    unknown = sorted(t.keys() - WATCH_KEYS)
    if unknown:
        raise SettingsError(
            f"[[watch]] has unknown {', '.join(unknown)}; use {', '.join(sorted(WATCH_KEYS))}."
        )
    name = _str(t, "name", "watch")
    if not _NAME_RE.match(name):
        raise SettingsError(
            f"watch.name {name!r} must be 1-40 letters, digits, spaces, '-' or '_', "
            "starting with a letter or digit."
        )
    section = f"watch {name!r}"
    preset_name = _str(t, "preset", section, default="") or None
    if preset_name is not None and preset_name not in PRESETS:
        raise SettingsError(f"{section}: preset must be one of {', '.join(PRESETS)}.")
    preset = PRESETS[preset_name] if preset_name else None

    phrases = _phrases(t, "search_phrases", section)
    if not phrases:
        if preset is None:
            raise SettingsError(f"{section}: search_phrases is required without a preset.")
        phrases = list(preset.search_phrases)
    if len(phrases) > MAX_PHRASES:
        # Each phrase is a page load per interval; Facebook throttles busy sessions.
        raise SettingsError(f"{section}: use at most {MAX_PHRASES} search_phrases.")

    if "keywords" in t:
        keywords = [quote_phrase(k) for k in _phrases(t, "keywords", section)]
    else:
        keywords = list(preset.keywords) if preset else []

    if "allow_barebones" in t and not (preset and preset.barebones_antikeywords):
        raise SettingsError(f"{section}: allow_barebones only applies to the mini-pc presets.")

    if "max_price" not in t:
        raise SettingsError(f"{section}: max_price is required.")
    min_price = _int(t, "min_price", section, default=0, lo=0, hi=1_000_000)
    max_price = _int(t, "max_price", section, default=0, lo=1, hi=1_000_000)
    if min_price >= max_price:
        raise SettingsError(f"{section}: min_price must be less than max_price.")

    return WatchSettings(
        name=name,
        preset=preset_name,
        search_phrases=phrases,
        keywords=keywords,
        antikeywords=_phrases(t, "antikeywords", section),
        min_price=min_price,
        max_price=max_price,
        preferences=_str(t, "preferences", section, default=""),
        rating=_int(t, "rating", section, default=default_rating, lo=1, hi=5),
        allow_barebones=_bool(t, "allow_barebones", section, default=True),
    )


def _legacy_watches(search: dict[str, Any], rating: int) -> list[WatchSettings]:
    """The [search]-only format: a mini-PC watch, plus tiny PCs if asked for.

    The names are the item names aimm used then, so cached ratings still apply.
    """
    min_price = _int(search, "min_price", "search", default=40, lo=0, hi=100_000)
    max_price = _int(search, "max_price", "search", default=400, lo=1, hi=100_000)
    if min_price >= max_price:
        raise SettingsError("search.min_price must be less than search.max_price.")
    presets = {"nuc": "mini-pc"}
    if _bool(search, "include_tiny_pcs", "search", default=False):
        presets["tiny_pc"] = "tiny-pc"
    return [
        WatchSettings(
            name=name,
            preset=preset,
            search_phrases=list(PRESETS[preset].search_phrases),
            keywords=list(PRESETS[preset].keywords),
            antikeywords=_phrases(search, "extra_antikeywords", "search"),
            min_price=min_price,
            max_price=max_price,
            preferences=_str(search, "preferences", "search", default=""),
            rating=rating,
            allow_barebones=_bool(search, "allow_barebones", "search", default=True),
        )
        for name, preset in presets.items()
    ]


def _phrases(t: dict[str, Any], key: str, section: str) -> list[str]:
    """Literal phrases; quotes are dropped, since they'd change aimm's matching."""
    values = [v.replace('"', "").strip() for v in _str_list(t, key, section)]
    # An empty phrase would match (or search for) every listing.
    if any(not v for v in values):
        raise SettingsError(f"{section}.{key} entries must not be empty.")
    return values


def quote_phrase(phrase: str) -> str:
    """A phrase as an aimm keyword literal, never an expression."""
    return f'"{phrase}"'


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
