"""The Prometheus exposition built from the dashboard's snapshot."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from fakes import LOGIN_COOKIE, FakeContext

from deal_hunter import metrics
from deal_hunter import session as fb_session
from deal_hunter.dashboard import Result, Snapshot
from deal_hunter.settings import Settings, parse_settings

NOW = 1_800_000_000.0
LOCATION = "springfieldtest"
PHRASES = ["secret phrase one", "secret phrase two"]
SECRET = "sk-test-fake-key-xxxxxxxx"  # noqa: S105 — a made-up key, low entropy for gitleaks

Samples = list[tuple[dict[str, str], float]]


def parse(text: str) -> dict[str, tuple[str, Samples]]:
    """A strict reader of the 0.0.4 text format: family -> (type, samples).

    Fails on anything the format doesn't allow: a sample before its TYPE, a
    family split in two, a duplicate series, a bad escape or number.
    """
    assert text.endswith("\n") and not text.endswith("\n\n")
    families: dict[str, tuple[str, Samples]] = {}
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    current: str | None = None
    helped: set[str] = set()
    for line in text.splitlines():
        if line.startswith("# HELP "):
            name = line.split(" ", 3)[2]
            assert name not in helped, f"{name} described twice"
            helped.add(name)
        elif line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert kind in ("counter", "gauge") and name not in families, line
            assert name in helped, f"{name} has no HELP"
            families[name] = (kind, [])
            current = name
        else:
            match = re.fullmatch(r"([a-z_:][a-z0-9_:]*)(?:\{(.*)\})? (\S+)", line)
            assert match, f"bad line: {line!r}"
            name, raw, value = match.groups()
            assert name == current, f"{name} outside its family {current}"
            labels: dict[str, str] = {}
            rest = raw or ""
            while rest:
                pair = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\["\\n])*)"(?:,|$)', rest)
                assert pair, f"bad labels: {raw!r}"
                labels[pair.group(1)] = (
                    pair.group(2).replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
                )
                rest = rest[pair.end() :]
            key = (name, tuple(sorted(labels.items())))
            assert key not in seen, f"duplicate series {key}"
            seen.add(key)
            families[name][1].append((labels, float(value)))
    return families


def value(families: dict[str, tuple[str, Samples]], name: str, **labels: str) -> float | None:
    """The one sample of name whose labels include these, or None if there is none."""
    if name not in families:
        return None
    found = [v for lab, v in families[name][1] if labels.items() <= lab.items()]
    assert len(found) <= 1, (name, labels)
    return found[0] if found else None


def total(families: dict[str, tuple[str, Samples]], name: str, **labels: str) -> float:
    return sum(v for lab, v in families[name][1] if labels.items() <= lab.items())


@pytest.fixture
def settings(base_settings: dict[str, Any], tmp_path: Path) -> Settings:
    base_settings["location"]["search_city"] = LOCATION
    base_settings["search"] = {"interval": "1h", "max_interval": "2h"}
    base_settings["watch"] = [
        {"name": "desk box", "search_phrases": PHRASES, "max_price": 300},
        {"name": "lamp", "search_phrases": ["secret lamp phrase"], "max_price": 50},
    ]
    base_settings["notify"] = {"methods": ["ntfy", "discord"]}
    base_settings["facebook"] = {"session_file": str(tmp_path / "fb.json")}
    return parse_settings(base_settings)


def snapshot(
    settings: Settings,
    events: list[dict[str, Any]],
    results: list[Result] | None = None,
    errors: list[str] | None = None,
) -> Snapshot:
    return Snapshot(
        results=results or [],
        counters={},
        events=events,
        now=NOW,
        errors=errors or [],
        items=[w.name for w in settings.watches],
    )


def result_(n: int, score: int | None = None, dropped: list[str] | None = None) -> Result:
    return Result(
        id=str(n),
        title=f"Secret title {n}",
        price="$1",
        location="Secret town",
        condition="",
        description="",
        image="",
        updated_at=NOW,
        score=score,
        watch="desk box",
        dropped=dropped or [],
    )


def results_event(ts: float, item: str, index: int, count: int) -> dict[str, Any]:
    return {"ts": ts, "kind": "search_results", "item": item, "phrase_index": index, "count": count}


def summary_event(ts: float, item: str) -> dict[str, Any]:
    return {"ts": ts, "kind": "search_summary", "item": item, "new_count": 0}


def scrape(settings: Settings, events: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return parse(metrics.render(snapshot(settings, events, **kw), settings))


# --- Format ------------------------------------------------------------------


def test_build_info_and_text_format(settings: Settings) -> None:
    text = metrics.render(snapshot(settings, []), settings)
    families = parse(text)
    labels = families["deal_hunter_build_info"][1][0][0]
    assert set(labels) == {"version", "aimm_version"}
    assert value(families, "deal_hunter_build_info") == 1
    assert families["deal_hunter_searches_total"][0] == "counter"
    assert families["deal_hunter_listings"][0] == "gauge"
    assert metrics.CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"


def test_label_values_are_escaped(settings: Settings) -> None:
    out = metrics._Exposition()
    out.family("x", "gauge", "help\nwith \\ newline", [({"a": 'q"\\\nz'}, 1)])
    families = parse(out.text())
    assert families["deal_hunter_x"][1][0][0] == {"a": 'q"\\\nz'}
    assert "\n" not in out.text().splitlines()[2].split("{", 1)[1]


# --- Searches ------------------------------------------------------------------


def test_a_round_with_two_of_six_phrases_failed(settings: Settings) -> None:
    counts = [3, 0, 5, 2, 0, 1]
    events = [results_event(NOW - 600 + i, "desk box", i, c) for i, c in enumerate(counts)]
    events.append(summary_event(NOW - 500, "desk box"))
    families = scrape(settings, events)
    assert total(families, "deal_hunter_searches_total", outcome="failed") == 2
    assert total(families, "deal_hunter_searches_total", outcome="ok") == 4
    assert value(families, "deal_hunter_last_success_timestamp_seconds") == NOW - 500
    assert value(
        families, "deal_hunter_watch_last_success_timestamp_seconds", watch="desk box"
    ) == (NOW - 500)
    assert value(families, "deal_hunter_watch_last_success_timestamp_seconds", watch="lamp") is None
    # Every watch has both outcomes, so increase() has a series to start from.
    assert value(families, "deal_hunter_searches_total", watch="lamp", outcome="failed") == 0


def test_a_zero_count_is_a_failed_search_with_no_phrase_or_location(settings: Settings) -> None:
    text = metrics.render(snapshot(settings, [results_event(NOW - 5, "lamp", 0, 0)]), settings)
    families = parse(text)
    assert value(families, "deal_hunter_searches_total", watch="lamp", outcome="failed") == 1
    assert value(families, "deal_hunter_searches_total", watch="lamp", outcome="ok") == 0
    assert "secret" not in text and LOCATION not in text


def test_a_round_where_every_phrase_failed_is_not_a_success(settings: Settings) -> None:
    events = [
        results_event(NOW - 900, "desk box", 0, 2),
        summary_event(NOW - 890, "desk box"),
        results_event(NOW - 100, "desk box", 0, 0),
        results_event(NOW - 99, "desk box", 1, 0),
        summary_event(NOW - 90, "desk box"),
    ]
    families = scrape(settings, events)
    assert value(families, "deal_hunter_last_success_timestamp_seconds") == NOW - 890


def test_the_heartbeat_is_the_newest_watch(settings: Settings) -> None:
    events = [
        results_event(NOW - 900, "desk box", 0, 2),
        summary_event(NOW - 890, "desk box"),
        results_event(NOW - 400, "lamp", 0, 1),
        summary_event(NOW - 390, "lamp"),
    ]
    families = scrape(settings, events)
    assert value(families, "deal_hunter_last_success_timestamp_seconds") == NOW - 390
    assert value(
        families, "deal_hunter_watch_last_success_timestamp_seconds", watch="desk box"
    ) == (NOW - 890)


def test_old_events_without_search_results_fall_back_to_search_summary(
    settings: Settings,
) -> None:
    families = scrape(settings, [summary_event(NOW - 700, "desk box")])
    assert value(families, "deal_hunter_last_success_timestamp_seconds") == NOW - 700
    assert total(families, "deal_hunter_searches_total") == 0  # counters start at the first event


def test_no_heartbeat_before_the_first_success(settings: Settings) -> None:
    families = scrape(settings, [])
    assert "deal_hunter_last_success_timestamp_seconds" not in families
    assert "deal_hunter_watch_last_success_timestamp_seconds" not in families
    assert "deal_hunter_searches_total" in families


# --- Listings, AI, notifications, problems ---------------------------------------


def test_listing_gauges(settings: Settings) -> None:
    rows = [
        result_(1, score=5),
        result_(2, score=2),
        result_(3, score=5, dropped=["price"]),
        result_(4),
    ]
    families = scrape(settings, [], results=rows)
    state = lambda s: value(families, "deal_hunter_listings", state=s)  # noqa: E731
    assert (state("fetched"), state("rated"), state("filtered_out"), state("matched")) == (
        4,
        2,
        1,
        1,
    )
    assert value(families, "deal_hunter_cache_read_errors") == 0


def test_an_unreadable_cache_drops_the_cache_series_and_says_so(settings: Settings) -> None:
    errors = ["Couldn't read the monitor's cache: database disk image is malformed (/home/x)"]
    text = metrics.render(snapshot(settings, [], errors=errors), settings)
    families = parse(text)
    assert "deal_hunter_listings" not in families
    assert value(families, "deal_hunter_cache_read_errors") == 1
    assert "malformed" not in text and "/home" not in text


def usage(model: str | None, inp: int = 1000, out: int = 500, **kw: Any) -> dict[str, Any]:
    return {
        "ts": NOW - 10,
        "kind": "ai_usage",
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        **kw,
    }


def test_ai_series(settings: Settings) -> None:
    events = [
        usage(
            "claude-haiku-4-5-20251001", 1_000_000, 0, cache_read_tokens=10, cache_write_tokens=20
        ),
        usage("claude-haiku-4-5-20251001", 0, 1_000_000),
        usage("mystery-model", 7, 3),
    ]
    families = scrape(settings, events)
    haiku = "claude-haiku-4-5-20251001"
    assert value(families, "deal_hunter_ai_calls_total", model=haiku) == 2
    assert value(families, "deal_hunter_ai_tokens_total", model=haiku, kind="input") == 1_000_000
    assert value(families, "deal_hunter_ai_tokens_total", model=haiku, kind="cache_read") == 10
    assert value(families, "deal_hunter_ai_tokens_total", model=haiku, kind="cache_write") == 20
    cost = value(families, "deal_hunter_ai_cost_dollars_total", model=haiku)
    assert cost == pytest.approx(1.0 + 5.0 + (10 * 0.1 + 20 * 1.25) / 1_000_000)
    assert value(families, "deal_hunter_ai_calls_total", model="mystery-model") == 1
    assert value(families, "deal_hunter_ai_cost_dollars_total", model="mystery-model") is None


def test_hostile_events_are_not_counted_or_used_as_labels(settings: Settings) -> None:
    events: list[dict[str, Any]] = [
        usage('evil"model\n{x="y"}'),
        usage(None),
        usage("claude-haiku-4-5", inp="9" * 30, out=-5),  # type: ignore[arg-type]
        usage("claude-haiku-4-5", inp=True, out=1.5),  # type: ignore[arg-type]
        results_event(NOW, "not a watch", 0, 3),
        results_event(NOW, "desk box", 0, "3"),  # type: ignore[arg-type]
        results_event(NOW, "desk box", 0, -1),
        results_event(NOW, ["desk box"], 0, 1),  # type: ignore[arg-type]
        results_event(NOW, "desk box", 0, 10**30),
        summary_event(float("nan"), "desk box"),
        summary_event(10**30, "desk box"),
        summary_event(-1, "desk box"),
        {"ts": NOW, "kind": "notification", "method": "evil\nmethod", "outcome": "sent"},
        {"ts": NOW, "kind": "notification", "method": "ntfy", "outcome": "weird"},
        {"ts": NOW, "kind": "error", "message": "not used"},
    ]
    text = metrics.render(snapshot(settings, events), settings)
    families = parse(text)
    models = {lab["model"] for lab, _ in families["deal_hunter_ai_calls_total"][1]}
    assert models == {"other", "unknown", "claude-haiku-4-5"}
    assert total(families, "deal_hunter_searches_total") == 0
    assert "deal_hunter_last_success_timestamp_seconds" not in families
    assert value(families, "deal_hunter_notifications_total", method="other", outcome="sent") == 1
    assert total(families, "deal_hunter_notifications_total", outcome="weird") == 0
    assert "evil" not in text and "not a watch" not in text


def test_distinct_models_are_capped(settings: Settings) -> None:
    events = [usage(f"model-{n}") for n in range(metrics.MAX_MODELS + 10)]
    families = scrape(settings, events)
    labels = {lab["model"] for lab, _ in families["deal_hunter_ai_calls_total"][1]}
    assert len(labels) == metrics.MAX_MODELS + 1 and "other" in labels
    assert value(families, "deal_hunter_ai_calls_total", model="other") == 10


def test_notifications_and_problems(settings: Settings) -> None:
    events = [
        {"ts": NOW, "kind": "notification", "method": "ntfy", "outcome": "sent"},
        {"ts": NOW, "kind": "notification", "method": "ntfy", "outcome": "sent"},
        {"ts": NOW, "kind": "notification", "method": "ntfy", "outcome": "failed"},
        {"ts": NOW, "kind": "notification", "method": "email", "outcome": "sent"},
        {"ts": NOW, "kind": "error", "message": "x"},
        {"ts": NOW, "kind": "warning", "message": "y"},
        {"ts": NOW, "kind": "warning", "message": "z"},
    ]
    families = scrape(settings, events)
    sent = lambda m, o: value(families, "deal_hunter_notifications_total", method=m, outcome=o)  # noqa: E731
    assert (sent("ntfy", "sent"), sent("ntfy", "failed"), sent("email", "sent")) == (2, 1, 1)
    assert sent("discord", "failed") == 0  # configured, never tried
    assert value(families, "deal_hunter_problems_total", kind="error") == 1
    assert value(families, "deal_hunter_problems_total", kind="warning") == 2


# --- Session and configuration -------------------------------------------------


def test_session_series_from_metadata(settings: Settings) -> None:
    families = scrape(settings, [])
    assert value(families, "deal_hunter_session_present") == 0  # no metadata yet
    assert "deal_hunter_session_saved_timestamp_seconds" not in families
    assert "deal_hunter_session_cookie_expiry_timestamp_seconds" not in families
    cookie = {**LOGIN_COOKIE, "expires": NOW + 2 * 86_400}
    fb_session.save(FakeContext([cookie]), settings.session_file)
    families = scrape(settings, [])
    assert value(families, "deal_hunter_session_present") == 1
    assert (
        value(families, "deal_hunter_session_cookie_expiry_timestamp_seconds") == NOW + 2 * 86_400
    )
    assert value(families, "deal_hunter_session_saved_timestamp_seconds") is not None


def test_anonymous_mode_exports_no_session_series(
    base_settings: dict[str, Any], settings: Settings
) -> None:
    base_settings["facebook"] = {"anonymous": True}
    anonymous = parse_settings(base_settings)
    text = metrics.render(snapshot(anonymous, []), anonymous)
    assert "session" not in text.replace("deal_hunter_", "")


def test_junk_session_metadata_is_not_exported(settings: Settings) -> None:
    fb_session.meta_path(settings.session_file).write_text(
        json.dumps({"saved_at": "yesterday", "expires": 10**30})
    )
    families = scrape(settings, [])
    assert value(families, "deal_hunter_session_present") == 0
    assert "deal_hunter_session_cookie_expiry_timestamp_seconds" not in families


def test_search_interval_comes_from_settings(settings: Settings) -> None:
    families = scrape(settings, [])
    assert value(families, "deal_hunter_search_interval_seconds", bound="min") == 3600
    assert value(families, "deal_hunter_search_interval_seconds", bound="max") == 7200


def test_unavailable_snapshot_reports_no_details(settings: Settings) -> None:
    text = metrics.render_unavailable(settings)
    families = parse(text)
    assert value(families, "deal_hunter_cache_read_errors") == 1
    assert "deal_hunter_searches_total" not in families


# --- Hygiene and cost -----------------------------------------------------------


def test_the_whole_output_carries_no_phrases_location_or_secrets(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    events = [
        results_event(NOW - 5, "desk box", 0, 2),
        results_event(NOW - 4, "desk box", 1, 0),
        summary_event(NOW - 3, "desk box"),
        usage("claude-haiku-4-5-20251001"),
        {"ts": NOW, "kind": "warning", "message": f"No results for {PHRASES[0]} in {LOCATION}"},
        {"ts": NOW, "kind": "notification", "method": "ntfy", "outcome": "sent"},
    ]
    fb_session.save(FakeContext([{**LOGIN_COOKIE, "expires": NOW + 86_400}]), settings.session_file)
    text = metrics.render(snapshot(settings, events, results=[result_(1, 5)]), settings)
    parse(text)
    from deal_hunter.render import build_config

    config = build_config(settings)
    phrases = {p for item in config["item"].values() for p in item["search_phrases"]}
    secrets = [SECRET, LOGIN_COOKIE["value"], "Secret title", "Secret town", "fbcdn", "http"]
    for needle in [*phrases, LOCATION, *secrets, str(settings.session_file)]:
        assert needle not in text, needle
    # And the only label values are the watches, enumerations, model and versions.
    allowed = {"desk box", "lamp", "ok", "failed", "sent", "ntfy", "discord", "min", "max"}
    allowed |= {"fetched", "rated", "filtered_out", "matched", "input", "output", "cache_read"}
    allowed |= {"cache_write", "error", "warning", "claude-haiku-4-5-20251001"}
    for _, samples in parse(text).values():
        for labels, _ in samples:
            for key, label in labels.items():
                if key not in ("version", "aimm_version"):
                    assert label in allowed, (key, label)


def test_twenty_thousand_listings_render_quickly(settings: Settings) -> None:
    rows = [
        result_(n, score=n % 5 + 1, dropped=["x"] if n % 7 == 0 else None) for n in range(20_000)
    ]
    events = [results_event(NOW - n, "desk box", n % 2, n % 3) for n in range(5_000)]
    events += [usage("claude-haiku-4-5") for _ in range(5_000)]
    snap = snapshot(settings, events, results=rows)
    started = time.perf_counter()
    text = metrics.render(snap, settings)
    assert time.perf_counter() - started < 2.0
    assert value(parse(text), "deal_hunter_listings", state="fetched") == 20_000


# --- Forged and aborted events -----------------------------------------------------


def test_a_future_timestamp_cannot_pin_the_heartbeat(settings: Settings) -> None:
    events = [
        results_event(NOW - 900, "desk box", 0, 2),
        summary_event(NOW - 890, "desk box"),
        results_event(NOW, "desk box", 0, 2),
        summary_event(NOW + 10 * 86_400, "desk box"),  # forged
    ]
    families = scrape(settings, events)
    assert value(families, "deal_hunter_last_success_timestamp_seconds") == NOW - 890
    near = scrape(
        settings,
        [*events[:2], results_event(NOW, "desk box", 0, 1), summary_event(NOW + 200, "desk box")],
    )
    assert value(near, "deal_hunter_last_success_timestamp_seconds") == NOW + 200  # clock skew


def test_a_round_aborted_by_a_restart_does_not_count_toward_the_next(settings: Settings) -> None:
    events = [
        results_event(NOW - 900, "desk box", 0, 4),  # found something, then the monitor died
        {"ts": NOW - 800, "kind": "browser_ready"},
        results_event(NOW - 700, "desk box", 0, 0),
        summary_event(NOW - 690, "desk box"),
    ]
    assert "deal_hunter_last_success_timestamp_seconds" not in scrape(settings, events)


def test_a_failed_refresh_is_reported_on_the_scrape(settings: Settings) -> None:
    text = metrics.render(snapshot(settings, []), settings, refresh_failed=True)
    assert value(parse(text), "deal_hunter_cache_read_errors") == 1
