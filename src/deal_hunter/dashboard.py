"""`deal-hunter web`: a read-only page with what the monitor found and how it's doing.

Everything comes from files the monitor writes in aimm's home:

- ``cache.db`` (aimm's diskcache): listing details, AI ratings, who was
  notified and aimm's lifetime counters. Opened read-only.
- ``deal-hunter-events.jsonl`` (events.py): timestamped searches, ratings,
  token usage and problems.
- the saved Facebook session's metadata: when it was saved and when its
  login cookie expires. Never its contents.

The page has no login, so it's served on 127.0.0.1 only unless told
otherwise, and rejects requests whose Host header isn't one it serves
(DNS rebinding). It runs no scripts, and seller text is HTML-escaped.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import html
import io
import json
import os
import pickle  # noqa: S403 — restricted to plain data, see _PlainUnpickler
import re
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from importlib.metadata import version
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from deal_hunter import session as fb_session
from deal_hunter.events import events_path, read_events
from deal_hunter.settings import Settings

# $ per million tokens (input, output), by model-name prefix. Cache reads are
# billed at a tenth of input, cache writes at 1.25x.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-5": (5.0, 25.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
}
DAY = 86_400
MAX_TS = 4_102_444_800  # 2100-01-01: anything later is junk, not a date
# A search can run late by this much before the monitor counts as stalled.
STALL_GRACE_S = 30 * 60
_ITEM_URL = re.compile(r"^\d{1,25}$")
_PRICE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


@dataclass
class Result:
    id: str
    title: str
    price: str
    location: str
    condition: str
    description: str
    image: str
    updated_at: float | None
    score: int | None = None
    comment: str | None = None
    rated_at: float | None = None
    notified: bool = False
    # The watch that rated it, or else the first whose filters it passes.
    watch: str | None = None
    # Why today's filters would drop it (rated before a filter change).
    dropped: list[str] = field(default_factory=list)

    @property
    def shown(self) -> bool:
        return self.score is not None and not self.dropped

    @property
    def url(self) -> str | None:
        if _ITEM_URL.match(self.id):
            return f"https://www.facebook.com/marketplace/item/{self.id}/"
        return None

    def image_url(self, now: float) -> str | None:
        """The photo, if it's on Facebook's CDN and its signed link hasn't expired."""
        if "\\" in self.image or "@" in self.image or any(c.isspace() for c in self.image):
            return None  # browsers and urlparse can disagree on the host
        parsed = urlparse(self.image)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host == "fbcdn.net" or host.endswith(".fbcdn.net")):
            return None
        # oe= is the link's expiry, as hex Unix time; after it the CDN returns 403.
        expiry = parse_qs(parsed.query).get("oe", [""])[0]
        try:
            if int(expiry, 16) < now:
                return None
        except ValueError:
            pass
        return self.image


@dataclass
class Snapshot:
    results: list[Result]
    counters: dict[str, int]
    events: list[dict[str, Any]]
    now: float
    cache_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


# --- Reading ---------------------------------------------------------------

# Importing aimm opens ~/.ai-marketplace-monitor/cache.db read-write (its utils
# module builds a Cache at import), which writes to the monitor's cache and
# fails on a read-only mount. The dashboard only needs aimm's code, so the
# first import runs with HOME pointing at a scratch directory. Kept for the
# life of the process, since aimm holds that cache open. session.AIMM_HOME is
# already bound to the real home by this module's top-level import of
# deal_hunter.session; keep that import at module level.
_SCRATCH_HOME: tempfile.TemporaryDirectory[str] | None = None


def import_aimm_without_cache() -> None:
    global _SCRATCH_HOME
    if "ai_marketplace_monitor.utils" in sys.modules:
        return
    scratch = tempfile.TemporaryDirectory(prefix="deal-hunter-web-")
    saved = os.environ.get("HOME")
    os.environ["HOME"] = scratch.name
    try:
        import ai_marketplace_monitor.listing  # noqa: F401

        import deal_hunter.filters  # noqa: F401 — imports the rest of aimm
    except BaseException:
        # Drop the half-imported modules, or the next call would skip the
        # workaround and a later import would open the real cache.
        for name in [m for m in sys.modules if m.startswith("ai_marketplace_monitor")]:
            del sys.modules[name]
        sys.modules.pop("deal_hunter.filters", None)
        sys.modules.pop("deal_hunter.launch", None)
        scratch.cleanup()
        raise
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved
    _SCRATCH_HOME = scratch


# Limits on what the dashboard reads from aimm's cache. The cache is written by
# the monitor, whose browser renders untrusted pages, so it's treated as
# untrusted input: no row may be big enough to exhaust memory.
MAX_ROWS = 20_000
MAX_ROW_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MODE_RAW, _MODE_PICKLE = 1, 4  # diskcache's storage modes


class _PlainUnpickler(pickle.Unpickler):
    """Loads only plain data (dict, list, tuple, str, numbers, …): no classes.

    aimm stores dicts of strings and numbers. A crafted pickle could otherwise
    run code in the dashboard.
    """

    def find_class(self, module: str, name: str) -> Any:
        raise pickle.UnpicklingError(f"refusing to load {module}.{name}")


def _plain_loads(data: bytes) -> Any:
    return _PlainUnpickler(io.BytesIO(data)).load()


def read_cache(home: Path) -> list[tuple[Any, Any, float]]:
    """(key, value, store_time) for every live entry in aimm's cache, read-only.

    Decodes diskcache's rows itself instead of through diskcache, which would
    unpickle anything and read value files from any path a row names. Rows
    stored in files (large values; aimm writes none) and oversized rows are
    skipped.
    """
    db = home / "cache.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT key, raw, store_time, mode, value FROM Cache "
            "WHERE (expire_time IS NULL OR expire_time > ?) AND filename IS NULL "
            "AND length(key) <= ? AND (value IS NULL OR length(value) <= ?) "
            "ORDER BY store_time DESC LIMIT ?",
            (time.time(), MAX_ROW_BYTES, MAX_ROW_BYTES, MAX_ROWS),
        )
        entries = _decode_rows(rows)  # newest first, so a cap drops the oldest
    finally:
        conn.close()
    entries.reverse()  # oldest first: later entries win in collect()
    return entries


def _decode_rows(rows: Iterable[tuple[Any, ...]]) -> list[tuple[Any, Any, float]]:
    """Decode rows until MAX_TOTAL_BYTES have been read; the rest are dropped."""
    entries = []
    budget = MAX_TOTAL_BYTES
    for key, raw, store_time, mode, value in rows:
        budget -= len(key) if isinstance(key, (bytes, str)) else 8
        budget -= len(value) if isinstance(value, (bytes, str)) else 8
        if budget < 0:
            break
        try:
            if not raw:
                key = _plain_loads(key)
            if mode == _MODE_RAW:
                decoded = bytes(value) if isinstance(value, memoryview) else value
            elif mode == _MODE_PICKLE and value is not None:
                decoded = _plain_loads(value)
            else:
                continue
            entries.append((key, decoded, store_time))
        except Exception:  # noqa: BLE001,S112 — skip an entry aimm wrote oddly
            continue
    return entries


def collect(settings: Settings, home: Path, now: float | None = None) -> Snapshot:
    from ai_marketplace_monitor.listing import Listing

    now = time.time() if now is None else now
    listings: dict[str, tuple[dict[str, Any], float]] = {}
    ratings: dict[str, tuple[dict[str, Any], float]] = {}
    notified: set[str] = set()
    counters: Counter[str] = Counter()
    errors: list[str] = []
    try:
        entries = read_cache(home)
    except sqlite3.Error as e:
        entries = []
        errors.append(f"Couldn't read the monitor's cache: {e}")
    for key, value, stored in entries:
        if not isinstance(key, tuple) or not key:
            continue
        kind = key[0]
        if kind == "listing-details" and isinstance(value, dict):
            listings[str(value.get("id", key[-1]))] = (value, stored)
        elif kind == "ai-inquiries" and len(key) == 4 and isinstance(value, dict):
            previous = ratings.get(key[3])
            if previous is None or stored > previous[1]:
                ratings[key[3]] = (value, stored)
        elif kind == "user-notifications" and len(key) >= 3:
            notified.add(str(key[2]))
        elif kind == "counters" and len(key) == 3 and isinstance(value, int):
            counters[str(key[1])] += value

    from deal_hunter.filters import drop_reasons, parse_price
    from deal_hunter.render import build_config

    config = build_config(settings)
    items = config["item"]
    names = list(items)
    seller_locations = config["marketplace"]["facebook"].get("seller_locations")
    results = []
    for listing_id, (details, stored) in listings.items():
        result = Result(
            id=listing_id,
            title=str(details.get("title", "")),
            price=str(details.get("price", "")),
            location=str(details.get("location", "")),
            condition=str(details.get("condition", "")),
            description=str(details.get("description", "")),
            image=str(details.get("image", "")),
            updated_at=stored,
            notified=listing_id in notified,
        )
        # aimm keys ratings by the listing's hash, which includes the search
        # item's (watch's) name; the cached details have it blank. A listing
        # two watches found has a rating from each: show the best one.
        best: tuple[int, float, str, str] | None = None
        for name in names:
            try:
                listing_hash = Listing(**{**details, "name": name}).hash
            except TypeError:
                break
            if rating := ratings.get(listing_hash):
                value, rated_at = rating
                score = value.get("score")
                if isinstance(score, int) and 1 <= score <= 5:
                    candidate = (score, rated_at, name, str(value.get("comment", "")))
                    best = max(best, candidate) if best else candidate
        if best is not None:
            result.score, result.rated_at, result.watch, result.comment = best
        text = f"{result.title}  {result.description}"
        per_item = {
            name: drop_reasons(
                item, text, parse_price(result.price), result.location, seller_locations
            )
            for name, item in items.items()
        }
        if result.watch is not None:
            # Judged by the watch that rated it, whatever the others say.
            result.dropped = per_item[result.watch]
        else:
            result.watch = next((name for name, reasons in per_item.items() if not reasons), None)
            if per_item and result.watch is None:
                result.dropped = next(iter(per_item.values()))
        results.append(result)
    cache_db = home / "cache.db"
    return Snapshot(
        results=results,
        counters=dict(counters),
        events=list(read_events(events_path(home))),
        now=now,
        cache_bytes=cache_db.stat().st_size if cache_db.is_file() else 0,
        errors=errors,
        items=names,
    )


# --- Metrics ---------------------------------------------------------------


def interval_seconds(text: str) -> int:
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([smhd])", text):
        total += int(amount) * {"s": 1, "m": 60, "h": 3600, "d": DAY}[unit]
    return total


def price_of(model: str | None) -> tuple[float, float] | None:
    for prefix, price in PRICES.items():
        if model and model.startswith(prefix):
            return price
    return None


def usage_cost(event: dict[str, Any]) -> float | None:
    price = price_of(event.get("model"))
    if price is None:
        return None
    per_in, per_out = price
    tokens = (
        event.get("input_tokens", 0) * per_in
        + event.get("cache_read_tokens", 0) * per_in * 0.1
        + event.get("cache_write_tokens", 0) * per_in * 1.25
        + event.get("output_tokens", 0) * per_out
    )
    return float(tokens) / 1_000_000


@dataclass
class Status:
    label: str  # Running, Starting, Stalled, Not running yet
    tone: str  # good, warn, bad, idle
    detail: str


def status(snap: Snapshot, settings: Settings) -> Status:
    starts = [e["ts"] for e in snap.events if e.get("kind") == "browser_ready"]
    searches = [e["ts"] for e in snap.events if e.get("kind") == "search_summary"]
    if not starts and not searches:
        return Status(
            "No activity yet",
            "idle",
            "Activity is recorded from the next `deal-hunter run`.",
        )
    last_start = max(starts, default=0.0)
    last_search = max(searches, default=0.0)
    allowed = interval_seconds(settings.search.max_interval) + STALL_GRACE_S
    if last_search < last_start:
        # A first run fetches and rates every listing, so it can take a while.
        if snap.now - last_start <= allowed:
            rated = sum(
                1 for e in snap.events if e.get("kind") == "ai_usage" and e["ts"] >= last_start
            )
            return Status(
                "Starting",
                "warn",
                f"Started {ago(last_start, snap.now)}; first search running"
                + (f" ({rated} AI calls so far)." if rated else "."),
            )
        return Status(
            "Stalled",
            "bad",
            f"Started {ago(last_start, snap.now)} but hasn't finished a search.",
        )
    if snap.now - last_search <= allowed:
        return Status("Running", "good", f"Last search {ago(last_search, snap.now)}.")
    return Status(
        "Stalled or stopped",
        "bad",
        f"No search since {ago(last_search, snap.now)}; one is expected every "
        f"{settings.search.interval}–{settings.search.max_interval}.",
    )


def summary(snap: Snapshot, settings: Settings) -> dict[str, Any]:
    """Everything the page shows, as plain data (also served as JSON)."""
    day_ago = snap.now - DAY
    week_ago = snap.now - 7 * DAY
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for event in snap.events:
        by_kind.setdefault(str(event.get("kind")), []).append(event)

    def recent(kind: str, since: float) -> list[dict[str, Any]]:
        return [e for e in by_kind.get(kind, []) if e["ts"] >= since]

    usage = by_kind.get("ai_usage", [])

    def cost(events: Iterable[dict[str, Any]]) -> float | None:
        # Calls with an unknown model's price are left out: see unpriced_calls.
        return sum(c for c in map(usage_cost, events) if c is not None)

    rated = [r for r in snap.results if r.shown]
    matches = [r for r in rated if is_match(r, settings)]
    searches = by_kind.get("search_summary", [])
    problems = [e for e in snap.events if e.get("kind") in ("error", "warning")]
    st = status(snap, settings)
    return {
        "generated_at": snap.now,
        "status": {"label": st.label, "tone": st.tone, "detail": st.detail},
        "last_search": max((e["ts"] for e in searches), default=None),
        "last_start": max((e["ts"] for e in by_kind.get("browser_ready", [])), default=None),
        "listings": {
            "seen": len(snap.results),
            "rated": len(rated),
            "filtered_out": sum(1 for r in snap.results if r.dropped),
            "matches": len(matches),
            "notified": sum(r.notified for r in snap.results),
            "updated_24h": sum(1 for r in snap.results if (r.updated_at or 0) >= day_ago),
        },
        "searches": {
            "last_24h": len(recent("search_summary", day_ago)),
            "last_7d": len(recent("search_summary", week_ago)),
            "new_listings_24h": sum(
                int(e.get("new_count") or 0) for e in recent("search_summary", day_ago)
            ),
        },
        "ai": {
            "calls_total": len(usage),
            "calls_24h": len(recent("ai_usage", day_ago)),
            "calls_7d": len(recent("ai_usage", week_ago)),
            "input_tokens": sum(int(e.get("input_tokens", 0)) for e in usage),
            "output_tokens": sum(int(e.get("output_tokens", 0)) for e in usage),
            "cost_total": cost(usage),
            "cost_24h": cost(recent("ai_usage", day_ago)),
            "cost_7d": cost(recent("ai_usage", week_ago)),
            "unpriced_calls": sum(1 for e in usage if usage_cost(e) is None),
            "model": settings.ai.model,
            "provider": settings.ai.provider,
        },
        "problems": {
            "errors_24h": len(recent("error", day_ago)),
            "warnings_24h": len(recent("warning", day_ago)),
            "recent": [
                {"ts": e["ts"], "kind": e["kind"], "message": e.get("message", "")}
                for e in problems[-12:][::-1]
            ],
        },
        "counters": snap.counters,
        "session": session_info(settings, snap.now),
        "app": {
            "deal_hunter": _version("deal-hunter"),
            "aimm": _version("ai-marketplace-monitor"),
            "search_city": settings.location.search_city,
            "radius": settings.location.radius,
            "seller_locations": len(settings.location.seller_locations),
            "interval": f"{settings.search.interval}–{settings.search.max_interval}",
            "rating": settings.ai.rating,
            "notify": settings.notify.methods,
            "watches": [
                {
                    "name": w.name,
                    "preset": w.preset,
                    "price": f"${w.min_price}–${w.max_price}",
                    "rating": w.rating,
                }
                for w in settings.watches
            ],
            "cache_bytes": snap.cache_bytes,
        },
        "errors": snap.errors,
    }


def session_info(settings: Settings, now: float) -> dict[str, Any]:
    """Login status from the session's metadata file; the session itself isn't read."""
    if settings.anonymous:
        return {"mode": "anonymous"}
    info: dict[str, Any] = {
        "mode": "required" if settings.require_session else "saved",
        "saved_at": None,
        "expires": None,
        "problem": None,
    }
    meta = fb_session.read_metadata(settings.session_file)
    if meta is None:
        if settings.session_file.exists():
            info["problem"] = "Saved; its details show up once `run` next logs in."
        else:
            info["problem"] = "No session details yet. Run `deal-hunter login` if you haven't."
        return info
    for key in ("saved_at", "expires"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and 0 < value < MAX_TS:
            info[key] = value
    if info["expires"] is not None and info["expires"] < now:
        info["problem"] = "The login cookie has expired. Run `deal-hunter login`."
    return info


def _version(dist: str) -> str:
    try:
        return version(dist)
    except Exception:  # noqa: BLE001
        return "unknown"


# --- Formatting ------------------------------------------------------------


def ago(ts: float | None, now: float) -> str:
    if ts is None or not 0 < ts < MAX_TS:
        return "never"
    seconds = max(0, int(now - ts))
    if seconds < 90:
        return "just now"
    for size, unit in ((DAY, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"  # pragma: no cover


def when(ts: float | None) -> str:
    if ts is None or not 0 < ts < MAX_TS:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%b %-d, %-I:%M %p")


def money(value: float | None, lower_bound: bool = False) -> str:
    if value is None:
        return "—"
    text = f"${value:.2f}" if value >= 0.01 or value == 0 else "<$0.01"
    return f"≥ {text}" if lower_bound else text


def number(n: int) -> str:
    return f"{n:,}"


def size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return ""  # pragma: no cover


def first_price(text: str) -> str:
    m = _PRICE.search(text)
    return f"${m.group(1)}" if m else text


# --- Page ------------------------------------------------------------------

VIEWS = {
    "matches": "Matches",
    "rated": "All rated",
    "all": "Everything fetched",
}


def is_match(r: Result, settings: Settings) -> bool:
    """Rated at or above its watch's notify rating (the default if the watch is gone)."""
    ratings = {w.name: w.rating for w in settings.watches}
    threshold = ratings.get(r.watch or "", settings.ai.rating)
    return r.shown and r.score is not None and r.score >= threshold


def select(
    results: list[Result], view: str, settings: Settings, watch: str | None = None
) -> list[Result]:
    if watch is not None:
        results = [r for r in results if r.watch == watch]
    if view == "matches":
        chosen = [r for r in results if is_match(r, settings)]
    elif view == "rated":
        chosen = [r for r in results if r.shown]
    else:
        chosen = list(results)
    return sorted(
        chosen,
        key=lambda r: (bool(r.dropped), -(r.score or 0), -(r.rated_at or r.updated_at or 0)),
    )


def e(value: object) -> str:
    return html.escape(str(value), quote=True)


def page_url(view: str, watch: str | None) -> str:
    return f"/?view={view}" + (f"&watch={quote(watch)}" if watch is not None else "")


def render_page(
    snap: Snapshot, settings: Settings, view: str = "matches", watch: str | None = None
) -> str:
    data = summary(snap, settings)
    if view not in VIEWS:
        view = "matches"
    names = [w.name for w in settings.watches]
    if watch not in names:
        watch = None
    shown = select(snap.results, view, settings, watch)
    counts = {key: len(select(snap.results, key, settings, watch)) for key in VIEWS}
    tabs = "".join(
        f'<a class="tab{" on" if key == view else ""}" href="{e(page_url(key, watch))}">'
        f"{e(label)} <span>{counts[key]}</span></a>"
        for key, label in VIEWS.items()
    )
    if len(names) > 1:
        chips = [(None, "All watches"), *((n, n) for n in names)]
        tabs += '<span class="tabsep"></span>' + "".join(
            f'<a class="tab watch{" on" if name == watch else ""}" '
            f'href="{e(page_url(view, name))}">{e(label)}</a>'
            for name, label in chips
        )
    ratings = sorted({w.rating for w in settings.watches})
    threshold = f"rated {ratings[0]}+" if len(ratings) == 1 else "at each watch's rating"
    st = data["status"]
    ai = data["ai"]
    cards = [
        ("Status", f'<span class="dot {e(st["tone"])}"></span>{e(st["label"])}', e(st["detail"])),
        (
            "Matches",
            number(data["listings"]["matches"]),
            f"{threshold} of {number(data['listings']['rated'])} rated",
        ),
        (
            "Searches (24h)",
            number(data["searches"]["last_24h"]),
            f"{number(data['searches']['last_7d'])} in 7 days · "
            f"last {ago(data['last_search'], snap.now)}",
        ),
        (
            "AI spend (7d)",
            money(ai["cost_7d"], bool(ai["unpriced_calls"])),
            f"{number(ai['calls_7d'])} calls · "
            f"{money(ai['cost_total'], bool(ai['unpriced_calls']))} since tracking began",
        ),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{e(k)}</div><div class="v">{v}</div>'
        f'<div class="s">{s}</div></div>'
        for k, v, s in cards
    )
    if shown:
        results_html = "".join(result_html(r, snap.now, len(names) > 1) for r in shown)
    else:
        results_html = (
            '<p class="empty">Nothing here yet. Matches show up once the AI rates a '
            "local listing at or above its watch's rating.</p>"
        )
    errors_html = "".join(f'<p class="banner">{e(msg)}</p>' for msg in data["errors"])
    return page_template().substitute(
        refresh=300,
        errors=errors_html,
        cards=cards_html,
        tabs=tabs,
        results=results_html,
        side=side_html(data, snap.now),
        generated=e(when(snap.now)),
    )


def stars(score: int) -> str:
    return "★" * score + "☆" * (5 - score)


def result_html(r: Result, now: float, show_watch: bool = False) -> str:
    image = r.image_url(now)
    img = (
        f'<img src="{e(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">'
        if image
        else '<div class="noimg">no photo</div>'
    )
    title = e(r.title or "(untitled)")
    if r.url:
        title = f'<a href="{e(r.url)}" target="_blank" rel="noopener noreferrer">{title}</a>'
    if r.score is not None:
        rating = (
            f'<div class="rating s{r.score}" title="AI rating {r.score} of 5">'
            f'<span class="stars">{stars(r.score)}</span> {r.score}/5</div>'
            f'<p class="comment">{e(r.comment or "")}</p>'
        )
    else:
        rating = '<div class="rating none">Not rated</div>'
    if r.dropped:
        rating += f'<p class="dropped">Filtered out: {e("; ".join(r.dropped))}</p>'
    badges = []
    if show_watch and r.watch:
        badges.append(f'<span class="badge watch">{e(r.watch)}</span>')
    if r.notified:
        badges.append('<span class="badge">notified</span>')
    if r.condition:
        badges.append(f'<span class="badge muted">{e(r.condition)}</span>')
    return (
        '<article class="result">'
        f'<div class="thumb">{img}</div>'
        '<div class="body">'
        f"<h3>{title}</h3>"
        f'<div class="meta"><strong>{e(first_price(r.price))}</strong>'
        f" · {e(r.location or 'unknown location')}"
        f" · updated {e(ago(r.updated_at, now))}</div>"
        f"{rating}"
        f'<div class="badges">{"".join(badges)}</div>'
        "</div></article>"
    )


def side_html(data: dict[str, Any], now: float) -> str:
    ai, app, sess, counters = data["ai"], data["app"], data["session"], data["counters"]

    def rows(pairs: list[tuple[str, str]]) -> str:
        return "".join(f"<tr><th>{e(k)}</th><td>{v}</td></tr>" for k, v in pairs)

    if sess["mode"] == "anonymous":
        login = [("Mode", "Anonymous (no login)")]
    else:
        login = [
            ("Mode", "Saved session, required" if sess["mode"] == "required" else "Saved session"),
            ("Saved", e(ago(sess["saved_at"], now)) if sess["saved_at"] else "—"),
            ("Cookie expires", e(when(sess["expires"]))),
        ]
        if sess["problem"]:
            login.append(("Problem", f'<span class="bad-text">{e(sess["problem"])}</span>'))

    activity = [
        ("Last start", e(ago(data["last_start"], now))),
        ("Last search", e(ago(data["last_search"], now))),
        ("New listings (24h)", number(data["searches"]["new_listings_24h"])),
        ("Listings fetched", number(data["listings"]["seen"])),
        ("Filtered out now", number(data["listings"]["filtered_out"])),
        ("Notified", number(data["listings"]["notified"])),
        ("Errors (24h)", number(data["problems"]["errors_24h"])),
        ("Warnings (24h)", number(data["problems"]["warnings_24h"])),
    ]
    spend = [
        ("Model", e(ai["model"] or ai["provider"])),
        ("Calls", f"{number(ai['calls_total'])} ({number(ai['calls_24h'])} in 24h)"),
        ("Tokens in / out", f"{number(ai['input_tokens'])} / {number(ai['output_tokens'])}"),
        ("Last 24h", money(ai["cost_24h"], bool(ai["unpriced_calls"]))),
        ("Last 7 days", money(ai["cost_7d"], bool(ai["unpriced_calls"]))),
        ("Since tracking began", money(ai["cost_total"], bool(ai["unpriced_calls"]))),
    ]
    if ai["unpriced_calls"]:
        spend.append(("No price for", f"{number(ai['unpriced_calls'])} calls (model unknown)"))
    lifetime = [(k, number(v)) for k, v in sorted(counters.items())]
    config = [
        ("Location", f"{e(app['search_city'])} · {app['radius']} mi"),
        ("Seller towns", number(app["seller_locations"]) if app["seller_locations"] else "any"),
        ("Search every", e(app["interval"])),
        ("Notify via", e(", ".join(app["notify"]) or "nothing")),
        *(
            (f"Watch: {w['name']}", f"{e(w['price'])} · rating {w['rating']}+")
            for w in app["watches"]
        ),
        ("Cache", e(size(app["cache_bytes"]))),
        ("Versions", f"deal-hunter {e(app['deal_hunter'])} · aimm {e(app['aimm'])}"),
    ]
    problems = data["problems"]["recent"]
    if problems:
        problems_html = (
            '<ul class="log">'
            + "".join(
                f'<li class="{e(p["kind"])}"><time>{e(when(p["ts"]))}</time>{e(p["message"])}</li>'
                for p in problems
            )
            + "</ul>"
        )
    else:
        problems_html = '<p class="muted small">No warnings or errors recorded.</p>'
    return (
        f"<section><h2>Activity</h2><table>{rows(activity)}</table></section>"
        f"<section><h2>Facebook login</h2><table>{rows(login)}</table></section>"
        f"<section><h2>AI usage</h2><table>{rows(spend)}</table>"
        '<p class="muted small">Estimated from token counts at list prices.</p></section>'
        f"<section><h2>Recent problems</h2>{problems_html}</section>"
        f"<section><h2>Lifetime counters</h2><table>{rows(lifetime) or rows([('—', '—')])}"
        "</table></section>"
        f"<section><h2>Settings</h2><table>{rows(config)}</table></section>"
    )


@functools.cache
def page_template() -> Template:
    return Template(resources.files("deal_hunter").joinpath("dashboard.html").read_text())


@functools.cache
def style_hash() -> str:
    """CSP hash of the page's one <style> block, so no other styles can run."""
    text = page_template().template
    css = text[text.index("<style>") + len("<style>") : text.index("</style>")]
    return "sha256-" + base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()


# --- Server ----------------------------------------------------------------


def security_headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; img-src https://*.fbcdn.net; style-src '{style_hash()}'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ),
        **_STATIC_HEADERS,
    }


_STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
LOOPBACK = ("127.0.0.1", "localhost", "::1")
WILDCARD = ("0.0.0.0", "::", "")  # noqa: S104 — names to exclude, not to bind


def allowed_hosts(bind: str, extra: Iterable[str]) -> set[str]:
    hosts = {*LOOPBACK, *extra}
    if bind not in WILDCARD:  # nobody browses to 0.0.0.0
        hosts.add(bind)
    allowed = set()
    for host in hosts:
        name = host.lower().strip("[]")
        allowed.add(name)
    return allowed


def host_of(header: str | None) -> str | None:
    if not header:
        return None
    header = header.strip().lower()
    if header.startswith("["):  # [::1]:8765
        end = header.find("]")
        return header[1:end] if end > 0 else None
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class SnapshotCache:
    """collect(), reused while the monitor's files are unchanged (at most MAX_AGE_S)."""

    MAX_AGE_S = 30

    def __init__(self, settings: Settings, home: Path) -> None:
        self.settings, self.home = settings, home
        self.lock = threading.Lock()
        self.key: tuple[Any, ...] | None = None
        self.snap: Snapshot | None = None
        self.at = 0.0

    def _key(self) -> tuple[Any, ...]:
        paths = [self.home / "cache.db", self.home / "cache.db-wal", events_path(self.home)]
        meta = fb_session.meta_path(self.settings.session_file)
        stamps: list[tuple[int, int] | None] = []
        for path in [*paths, meta]:
            try:
                st = path.stat()
                stamps.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamps.append(None)
        return tuple(stamps)

    def get(self) -> Snapshot:
        with self.lock:
            key, now = self._key(), time.time()
            if self.snap is None or key != self.key or now - self.at > self.MAX_AGE_S:
                self.snap = collect(self.settings, self.home, now)
                # After, not before: the first read itself creates cache.db-wal.
                # A change during the read is picked up within MAX_AGE_S.
                self.key, self.at = self._key(), now
            return self.snap


class BoundedServer(ThreadingHTTPServer):
    """A threaded server that drops connections beyond MAX_THREADS at once."""

    daemon_threads = True
    MAX_THREADS = 8

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(self.MAX_THREADS)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_handler(settings: Settings, home: Path, hosts: set[str]) -> type[BaseHTTPRequestHandler]:
    snapshots = SnapshotCache(settings, home)

    class Handler(BaseHTTPRequestHandler):
        timeout = 15  # seconds a client may take to send its request
        # HTTP/1.1 (some health checkers reject 1.0), but every response
        # closes its connection — send() says so, and so does http.server's
        # send_error — so idle keep-alives can't hold the server's few threads.
        protocol_version = "HTTP/1.1"

        def version_string(self) -> str:
            return "deal-hunter"

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET(head=True)

        def do_GET(self, head: bool = False) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/healthz":  # says only "ok"; probes send the pod IP as Host
                self.send(HTTPStatus.OK, "text/plain", b"ok\n", head)
                return
            if host_of(self.headers.get("Host")) not in hosts:
                self.send(HTTPStatus.MISDIRECTED_REQUEST, "text/plain", b"Unknown host\n", head)
                return
            if self.headers.get("Sec-Fetch-Site") == "cross-site" and not (
                # Following a link (or a login proxy's redirect) here is fine:
                # the other site can't read or frame the page.
                self.headers.get("Sec-Fetch-Mode") == "navigate"
                and self.headers.get("Sec-Fetch-Dest") == "document"
            ):
                # Another website's page loading this one in the background.
                self.send(HTTPStatus.FORBIDDEN, "text/plain", b"Cross-site request\n", head)
                return
            if url.path not in ("/", "/api/summary.json"):
                self.send(HTTPStatus.NOT_FOUND, "text/plain", b"Not found\n", head)
                return
            snap = snapshots.get()
            if url.path == "/":
                query = parse_qs(url.query)
                view = query.get("view", ["matches"])[0]
                watch = query.get("watch", [None])[0]
                body = render_page(snap, settings, view, watch).encode()
                self.send(HTTPStatus.OK, "text/html; charset=utf-8", body, head)
            else:
                data = summary(snap, settings)
                data["results"] = [
                    {
                        "id": r.id,
                        "title": r.title,
                        "url": r.url,
                        "price": r.price,
                        "location": r.location,
                        "score": r.score,
                        "comment": r.comment,
                        "notified": r.notified,
                        "watch": r.watch,
                        "filtered_out": r.dropped,
                        "updated_at": r.updated_at,
                        "rated_at": r.rated_at,
                    }
                    for r in select(snap.results, "all", settings)
                ]
                body = json.dumps(data, indent=1).encode()
                self.send(HTTPStatus.OK, "application/json", body, head)

        def send(self, code: HTTPStatus, kind: str, body: bytes, head: bool) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for name, value in security_headers().items():
                self.send_header(name, value)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # quiet: the page refreshes itself every few minutes

    return Handler


def serve(
    settings: Settings, home: Path, host: str, port: int, extra_hosts: Iterable[str] = ()
) -> None:
    import_aimm_without_cache()
    hosts = allowed_hosts(host, extra_hosts)
    server = BoundedServer((host, port), make_handler(settings, home, hosts))
    shown = f"[{host}]" if ":" in host else host
    print(f"deal-hunter dashboard on http://{shown}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
