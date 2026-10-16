"""`deal-hunter web --metrics-port`: the dashboard's data as Prometheus metrics.

Everything is computed from the same Snapshot the dashboard shows (aimm's
cache and the events file, read once per refresh by ``dashboard.SnapshotCache``),
and rendered in the text exposition format 0.0.4 with no client library.

Counters are tallies over the events file, so they reset when the file is
trimmed (``events.MAX_BYTES``); ``rate()`` and ``increase()`` treat the drop as a
counter reset. They live in ``web`` rather than the monitor so that they
survive the monitor's restarts (it restarts by ``exec``).

The events file is written by the monitor, whose browser renders untrusted
pages, so it is treated as untrusted here: only values of the right type and
shape are counted, and a label value is either a configured watch name, a
fixed enumeration, or a model name that matches a strict pattern (the number
of distinct ones capped). No label or sample ever carries a search phrase, the
Marketplace location, a listing's text or URL, AI text or a secret.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from deal_hunter import dashboard
from deal_hunter.dashboard import MAX_TS, Snapshot
from deal_hunter.settings import DEAL_HUNTER_NOTIFIERS, NOTIFIERS, Settings

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
NAMESPACE = "deal_hunter"

SEARCH_OUTCOMES = ("ok", "failed")
NOTIFICATION_OUTCOMES = ("sent", "failed")
TOKEN_KINDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
}
NOTIFY_METHODS = (*NOTIFIERS, *DEAL_HUNTER_NOTIFIERS)
# What aimm and the providers call a model (events.ai_usage's own pattern).
_MODEL = re.compile(r"[\w.:/-]{1,80}")
MAX_MODELS = 20
# A count above this is junk, not a count (and keeps the sums exact).
_MAX_COUNT = 10**12

Labels = dict[str, str]
Sample = tuple[Labels, float]


def _natural(value: Any) -> int | None:
    """value if it is a plausible non-negative count, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        return None
    return value


# Clock skew allowed before an event counts as forged (seen from the future).
FUTURE_SLACK_S = 300


def _timestamp(value: Any, now: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 < value < MAX_TS:
        return None
    if now is not None and value > now + FUTURE_SLACK_S:
        return None
    return float(value)


@dataclass
class Tally:
    """What the events file says, already validated."""

    searches: Counter[tuple[str, str]] = field(default_factory=Counter)  # (watch, outcome)
    last_success: dict[str, float] = field(default_factory=dict)  # by watch
    notifications: Counter[tuple[str, str]] = field(default_factory=Counter)  # (method, outcome)
    problems: Counter[str] = field(default_factory=Counter)
    ai_calls: Counter[str] = field(default_factory=Counter)  # by model
    ai_tokens: Counter[tuple[str, str]] = field(default_factory=Counter)  # (model, kind)
    ai_cost: dict[str, float] = field(default_factory=dict)  # priced models only


def tally(
    events: Iterable[dict[str, Any]], watches: Iterable[str], now: float | None = None
) -> Tally:
    """Count the events, oldest first.

    A watch's search round ends with its ``search_summary`` event, after the
    ``search_results`` of each of its phrases. The round counts as a success
    (the heartbeat) if any of those phrases found something. Until a watch has
    recorded a single ``search_results`` (a file from before they existed), a
    summary alone counts, so the heartbeat starts from the old events. A
    monitor restart (``browser_ready``) abandons the round in progress, and a
    summary stamped later than ``now`` plus a little slack is ignored, so one
    forged event can't pin the heartbeat.
    """
    names = set(watches)
    out = Tally()
    found = dict.fromkeys(names, False)  # a phrase of the current round found listings
    counted = dict.fromkeys(names, False)  # this watch has recorded search_results
    models: set[str] = set()

    def model_label(raw: Any) -> str:
        if raw is None:
            return "unknown"
        if not isinstance(raw, str) or not _MODEL.fullmatch(raw):
            return "other"
        if raw not in models and len(models) >= MAX_MODELS:
            return "other"
        models.add(raw)
        return raw

    for event in events:
        kind = event.get("kind")
        item = event.get("item")
        if kind == "search_results" and isinstance(item, str) and item in names:
            count = _natural(event.get("count"))
            if count is None:
                continue
            counted[item] = True
            out.searches[item, "ok" if count > 0 else "failed"] += 1
            found[item] = found[item] or count > 0
        elif kind == "search_summary" and isinstance(item, str) and item in names:
            ts = _timestamp(event.get("ts"), now)
            if ts is not None and (found[item] or not counted[item]):
                out.last_success[item] = max(out.last_success.get(item, 0.0), ts)
            found[item] = False
        elif kind == "browser_ready":
            found = dict.fromkeys(names, False)
        elif kind == "notification":
            method, outcome = event.get("method"), event.get("outcome")
            if outcome in NOTIFICATION_OUTCOMES:
                label = method if method in NOTIFY_METHODS else "other"
                out.notifications[label, str(outcome)] += 1
        elif kind in ("error", "warning"):
            out.problems[str(kind)] += 1
        elif kind == "ai_usage":
            model = model_label(event.get("model"))
            clean = {"model": event.get("model") if model not in ("unknown", "other") else None}
            for name in TOKEN_KINDS.values():
                clean[name] = _natural(event.get(name)) or 0
            out.ai_calls[model] += 1
            for label, name in TOKEN_KINDS.items():
                out.ai_tokens[model, label] += clean[name]  # type: ignore[operator]
            cost = dashboard.usage_cost(clean)
            if cost is not None:
                out.ai_cost[model] = out.ai_cost.get(model, 0.0) + cost
    return out


# --- Exposition ------------------------------------------------------------


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


class _Exposition:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def family(self, name: str, kind: str, help_text: str, samples: list[Sample]) -> None:
        """One metric family; nothing at all if it has no samples (absent, not zero)."""
        if not samples:
            return
        full = f"{NAMESPACE}_{name}"
        self.lines.append(f"# HELP {full} {_escape_help(help_text)}")
        self.lines.append(f"# TYPE {full} {kind}")
        for labels, value in samples:
            if not math.isfinite(value):
                continue
            rendered = ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels.items())
            self.lines.append(
                f"{full}{{{rendered}}} {_number(value)}" if rendered else f"{full} {_number(value)}"
            )

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render(snap: Snapshot, settings: Settings, refresh_failed: bool = False) -> str:
    """The exposition text for a snapshot (refresh_failed: it is the last good one)."""
    # The names aimm knows the searches by: the watches', or a legacy [search]'s.
    names = snap.items or [w.name for w in settings.watches]
    counts = tally(snap.events, names, snap.now)
    out = _Exposition()

    _build_info(out)

    if counts.last_success:
        out.family(
            "last_success_timestamp_seconds",
            "gauge",
            "End of the latest search round in which a phrase of any watch returned listings.",
            [({}, max(counts.last_success.values()))],
        )
        out.family(
            "watch_last_success_timestamp_seconds",
            "gauge",
            "End of the watch's latest search round in which a phrase returned listings.",
            [({"watch": n}, counts.last_success[n]) for n in names if n in counts.last_success],
        )
    out.family(
        "searches_total",
        "counter",
        "Phrase searches by watch and outcome (failed means no listings came back).",
        [
            ({"watch": n, "outcome": o}, counts.searches[n, o])
            for n in names
            for o in SEARCH_OUTCOMES
        ],
    )

    if not snap.errors:  # an unreadable cache says nothing about the listings
        rated = [r for r in snap.results if r.shown]
        out.family(
            "listings",
            "gauge",
            "Listings in the monitor's cache, as the dashboard counts them.",
            [
                ({"state": "fetched"}, len(snap.results)),
                ({"state": "rated"}, len(rated)),
                ({"state": "filtered_out"}, sum(1 for r in snap.results if r.dropped)),
                ({"state": "matched"}, sum(1 for r in rated if dashboard.is_match(r, settings))),
            ],
        )
    out.family(
        "cache_read_errors",
        "gauge",
        "Problems reading the monitor's cache for this scrape (0 when it read cleanly).",
        [({}, len(snap.errors) + int(refresh_failed))],
    )

    models = sorted(counts.ai_calls)
    out.family(
        "ai_calls_total",
        "counter",
        "AI rating calls by model.",
        [({"model": m}, counts.ai_calls[m]) for m in models],
    )
    out.family(
        "ai_tokens_total",
        "counter",
        "AI tokens by model and kind.",
        [({"model": m, "kind": k}, counts.ai_tokens[m, k]) for m in models for k in TOKEN_KINDS],
    )
    out.family(
        "ai_cost_dollars_total",
        "counter",
        "Estimated AI spend at list prices; absent for models without a price.",
        [({"model": m}, counts.ai_cost[m]) for m in models if m in counts.ai_cost],
    )

    methods = [*dict.fromkeys([*settings.notify.methods, *(m for m, _ in counts.notifications)])]
    out.family(
        "notifications_total",
        "counter",
        "Notification attempts by method and outcome.",
        [
            ({"method": m, "outcome": o}, counts.notifications[m, o])
            for m in methods
            for o in NOTIFICATION_OUTCOMES
        ],
    )
    out.family(
        "problems_total",
        "counter",
        "Errors and warnings the monitor recorded.",
        [({"kind": k}, counts.problems[k]) for k in ("error", "warning")],
    )

    session = dashboard.session_info(settings, snap.now)
    if session["mode"] != "anonymous":
        saved, expires = session["saved_at"], session["expires"]
        out.family(
            "session_present",
            "gauge",
            "1 if the monitor has saved a Facebook session (its metadata exists), else 0.",
            [({}, 0 if saved is None else 1)],
        )
        out.family(
            "session_saved_timestamp_seconds",
            "gauge",
            "When the monitor last saved the Facebook session.",
            [] if saved is None else [({}, saved)],
        )
        out.family(
            "session_cookie_expiry_timestamp_seconds",
            "gauge",
            "When the Facebook login cookie expires.",
            [] if expires is None else [({}, expires)],
        )

    _search_interval(out, settings)
    return out.text()


def _build_info(out: _Exposition) -> None:
    out.family(
        "build_info",
        "gauge",
        "Versions of deal-hunter and the pinned ai-marketplace-monitor; always 1.",
        [
            (
                {
                    "version": dashboard._version("deal-hunter"),
                    "aimm_version": dashboard._version("ai-marketplace-monitor"),
                },
                1,
            )
        ],
    )


def _search_interval(out: _Exposition, settings: Settings) -> None:
    out.family(
        "search_interval_seconds",
        "gauge",
        "The configured time between searches.",
        [
            ({"bound": "min"}, dashboard.interval_seconds(settings.search.interval)),
            ({"bound": "max"}, dashboard.interval_seconds(settings.search.max_interval)),
        ],
    )


def render_unavailable(settings: Settings) -> str:
    """What a scrape gets when the snapshot itself can't be built: no details, no trace."""
    out = _Exposition()
    _build_info(out)
    out.family(
        "cache_read_errors",
        "gauge",
        "Problems reading the monitor's cache for this scrape (0 when it read cleanly).",
        [({}, 1)],
    )
    _search_interval(out, settings)
    return out.text()
