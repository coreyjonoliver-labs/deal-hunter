"""The monitor's event log, which `deal-hunter web` reads."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import pytest
from ai_marketplace_monitor.utils import aimm_event

from deal_hunter import events

AI_RESPONSE = """[blue][AI-Response][/blue] Message(
    id='msg_01',
    model='claude-haiku-4-5-20251001',
    usage=Usage(
        cache_creation_input_tokens=0,
        cache_read_input_tokens=200,
        input_tokens=1098,
        output_tokens=601,
    )
)"""


def record(message: str, level: int = logging.INFO, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("monitor", level, __file__, 1, message, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


@pytest.fixture
def recorder(tmp_path: Path) -> events.EventRecorder:
    return events.EventRecorder(tmp_path / events.EVENTS_NAME)


def test_structured_events_keep_known_fields(recorder: events.EventRecorder) -> None:
    extra = aimm_event("search_summary", item="nuc", marketplace="facebook", new_count=2)
    event = recorder.to_event(record("[Search] 2 new listings", **extra))
    assert event == {"ts": event["ts"], "kind": "search_summary", "item": "nuc", "new_count": 2}


@pytest.mark.parametrize("kind", ["credentials_wait", "ai_eval", "listing_skip"])
def test_unused_structured_kinds_are_ignored(recorder: events.EventRecorder, kind: str) -> None:
    assert recorder.to_event(record("x", **aimm_event(kind, title="t"))) is None


def test_ai_usage(recorder: events.EventRecorder) -> None:
    event = recorder.to_event(record(AI_RESPONSE, logging.DEBUG))
    assert event is not None
    assert event == {
        "ts": event["ts"],
        "kind": "ai_usage",
        "model": "claude-haiku-4-5-20251001",
        "input_tokens": 1098,
        "output_tokens": 601,
        "cache_read_tokens": 200,
        "cache_write_tokens": 0,
    }


def test_usage_ignores_the_models_own_text(recorder: events.EventRecorder) -> None:
    # A listing can get the AI to repeat anything; its text comes first.
    spoof = AI_RESPONSE.replace(
        "id='msg_01',",
        "id='msg_01', content=[TextBlock(text=\"model='fake' usage=Usage(input_tokens=9) "
        'input_tokens=900000000")],',
    )
    event = recorder.to_event(record(spoof, logging.DEBUG))
    assert event is not None
    assert event["model"] == "claude-haiku-4-5-20251001" and event["input_tokens"] == 1098


def test_openai_usage(recorder: events.EventRecorder) -> None:
    text = (
        "[blue][AI-Response][/blue] ChatCompletion(id='c', choices=[...], model='gpt-4o-mini', "
        "usage=CompletionUsage(completion_tokens=50, prompt_tokens=900, total_tokens=950))"
    )
    event = recorder.to_event(record(text, logging.DEBUG))
    assert event is not None
    assert (event["model"], event["input_tokens"], event["output_tokens"]) == (
        "gpt-4o-mini",
        900,
        50,
    )


def test_secrets_are_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEF")
    monkeypatch.setenv("NTFY_TOPIC", "nuc-alerts-x9q")
    monkeypatch.setenv("HOME_DIR_NAME", "keepme")
    rec = events.EventRecorder(tmp_path / "e.jsonl")
    event = rec.to_event(
        record("[Notify] failed for 123456:ABCDEF on nuc-alerts-x9q keepme", logging.ERROR)
    )
    assert event is not None
    assert event["message"] == "[Notify] failed for [redacted] on [redacted] keepme"
    encoded = rec.to_event(record("POST /bot123456%3AABCDEF/send failed", logging.ERROR))
    assert encoded is not None and "ABCDEF" not in encoded["message"]


def test_override_warning_is_dropped(recorder: events.EventRecorder) -> None:
    msg = "Overriding telegram_token for user me with value 123:abc from notification tg"
    assert recorder.to_event(record(msg, logging.WARNING)) is None


def test_prompt_text_mentioning_tokens_is_not_usage(recorder: events.EventRecorder) -> None:
    # Only aimm's [AI-Response] dump counts; seller text could say anything.
    assert recorder.to_event(record("[AI-Prompt] input_tokens=999999", logging.DEBUG)) is None


def test_problems_are_plain_and_short(recorder: events.EventRecorder) -> None:
    long = "[red][Search][/red] Failed " + "x" * 1000
    event = recorder.to_event(record(long, logging.ERROR))
    assert event is not None and event["kind"] == "error"
    assert event["message"].startswith("[Search] Failed")
    assert len(event["message"]) == events.MESSAGE_CHARS
    warning = recorder.to_event(record("[Login] expired", logging.WARNING))
    assert warning is not None and warning["kind"] == "warning"


def test_other_info_is_ignored(recorder: events.EventRecorder) -> None:
    assert recorder.to_event(record("[Search] Searching facebook")) is None


def test_written_owner_only_and_read_back(recorder: events.EventRecorder) -> None:
    recorder.emit(record("[Login] expired", logging.WARNING))
    recorder.emit(record("ignored"))
    assert stat.S_IMODE(recorder.path.stat().st_mode) == 0o600
    with recorder.path.open("a") as f:
        f.write('{"torn": ')  # a write cut off mid-line
    read = list(events.read_events(recorder.path))
    assert [e["kind"] for e in read] == ["warning"]


def test_torn_multibyte_line_is_skipped(recorder: events.EventRecorder) -> None:
    recorder.write({"ts": 1, "kind": "warning", "message": "ok"})
    with recorder.path.open("ab") as f:
        f.write('{"ts": 2, "message": "caf\u00e9'.encode()[:-1] + b"\n")  # cut mid-é
    recorder.write({"ts": 3, "kind": "warning", "message": "after"})
    assert [e["ts"] for e in events.read_events(recorder.path)] == [1, 3]


def test_missing_file_reads_empty(tmp_path: Path) -> None:
    assert list(events.read_events(tmp_path / "none.jsonl")) == []


def test_does_not_follow_symlink(recorder: events.EventRecorder, tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.write_text("keep me")
    recorder.path.symlink_to(target)
    logging.raiseExceptions = False
    try:
        recorder.emit(record("boom", logging.ERROR))  # must not raise into the monitor
    finally:
        logging.raiseExceptions = True
    assert target.read_text() == "keep me"


def test_trims_to_the_newest_half(
    recorder: events.EventRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(events, "MAX_BYTES", 2000)
    for i in range(100):
        recorder.write({"ts": i, "kind": "warning", "message": "m"})
    read = list(events.read_events(recorder.path))
    assert recorder.path.stat().st_size <= 2000 + 100
    assert read[-1]["ts"] == 99 and read[0]["ts"] > 0
    assert stat.S_IMODE(recorder.path.stat().st_mode) == 0o600


def test_attach_once(tmp_path: Path) -> None:
    logger = logging.getLogger("test-attach")
    try:
        events.attach(logger, tmp_path / "e.jsonl")
        events.attach(logger, tmp_path / "e.jsonl")
        assert sum(isinstance(h, events.EventRecorder) for h in logger.handlers) == 1
        logger.warning("[red][x][/red] hi")
        line = json.loads((tmp_path / "e.jsonl").read_text())
        assert line["message"] == "[x] hi"
    finally:
        logger.handlers.clear()


def test_home_directory_is_shortened(recorder: events.EventRecorder) -> None:
    msg = f"Couldn't exclude {Path.home()}/.ai-marketplace-monitor/s.json"
    event = recorder.to_event(record(msg, logging.WARNING))
    assert (
        event is not None
        and event["message"] == "Couldn't exclude ~/.ai-marketplace-monitor/s.json"
    )


def test_read_events_reads_only_the_newest_part(tmp_path: Path) -> None:
    from deal_hunter.events import LINE_CHARS, read_events

    path = tmp_path / "events.jsonl"
    old = [json.dumps({"ts": i, "kind": "x"}) for i in range(1000)]
    huge = json.dumps({"ts": 5000, "kind": "x", "message": "y" * LINE_CHARS})
    path.write_text("\n".join([*old, huge, json.dumps({"ts": 9999, "kind": "x"})]) + "\n")
    events = list(read_events(path, max_bytes=len(huge) + 600))
    assert events[-1]["ts"] == 9999
    older = [e["ts"] for e in events[:-1]]
    assert older and min(older) > 900  # only the tail, starting at a whole line
    assert 5000 not in [e["ts"] for e in events]  # the overlong line is skipped


def test_events_path_moves_the_old_named_file_on_run(tmp_path: Path) -> None:
    legacy = tmp_path / events.LEGACY_EVENTS_NAME
    legacy.write_text('{"ts": 1}\n')
    assert events.events_path(tmp_path) == legacy  # web reads it in place
    assert legacy.exists()
    path = events.events_path(tmp_path, migrate=True)
    assert path == tmp_path / events.EVENTS_NAME
    assert path.read_text() == '{"ts": 1}\n' and not legacy.exists()


def test_events_path_prefers_the_new_file(tmp_path: Path) -> None:
    (tmp_path / events.LEGACY_EVENTS_NAME).write_text("old\n")
    (tmp_path / events.EVENTS_NAME).write_text("new\n")
    assert events.events_path(tmp_path, migrate=True) == tmp_path / events.EVENTS_NAME
    assert (tmp_path / events.LEGACY_EVENTS_NAME).read_text() == "old\n"


def test_read_events_refuses_a_link(tmp_path: Path) -> None:
    target = tmp_path / "other.jsonl"
    target.write_text('{"ts": 1, "kind": "error", "message": "elsewhere"}\n')
    link = tmp_path / events.EVENTS_NAME
    link.symlink_to(target)
    assert list(events.read_events(link)) == []
    assert list(events.read_events(target))  # the same file, read directly


FALSE_FAIL = "[red][Search][/red] Failed to get search results for intel nuc from springfieldtest"


def search_record(message: str, level: int = logging.ERROR) -> logging.LogRecord:
    """A record as aimm's FacebookMarketplace.search logs it (module/function as measured)."""
    return logging.LogRecord("monitor", level, "/x/ai_marketplace_monitor/facebook.py", 524,
                             message, None, None, func="search")  # fmt: skip


@pytest.mark.parametrize("level", [logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG])
def test_aimms_false_search_failure_is_dropped(recorder: events.EventRecorder, level: int) -> None:
    # aimm 0.10.2 logs this after every search, whatever it found.
    assert recorder.to_event(search_record(FALSE_FAIL, level)) is None


def test_sentinel_text_from_a_listing_does_not_hide_real_errors(
    recorder: events.EventRecorder,
) -> None:
    # A seller can put the sentinel in a title; aimm logs titles in other places.
    title = "[Search] Failed to get search results for x from y"
    message = f"[red][AI-Error][/red] failed to evaluate {title}: boom"
    from_ai = logging.LogRecord(
        "monitor", logging.ERROR, "/x/ai_marketplace_monitor/ai.py", 1, message, None, None,
        func="evaluate",
    )  # fmt: skip
    event = recorder.to_event(from_ai)
    assert event is not None and event["kind"] == "error"
    # Same text from the search function, but not at the start of the message.
    assert recorder.to_event(search_record(message)) is not None
    # And the sentinel at the start, from another module.
    assert recorder.to_event(record(FALSE_FAIL, logging.ERROR)) is not None


def test_real_parse_error_is_still_recorded(recorder: events.EventRecorder) -> None:
    message = (
        "[red][Retrieve][/red] Failed to parse search results 2 listing: list index out of range"
    )
    event = recorder.to_event(record(message, logging.ERROR))
    assert event is not None
    assert event["kind"] == "error"
    assert event["message"] == (
        "[Retrieve] Failed to parse search results 2 listing: list index out of range"
    )


def test_search_results_event(recorder: events.EventRecorder) -> None:
    extra = aimm_event("search_results", item="nuc", phrase_index=1, count=3, phrase="secret")
    event = recorder.to_event(record("[Search] results", **extra))
    assert event == {
        "ts": event["ts"],
        "kind": "search_results",
        "item": "nuc",
        "phrase_index": 1,
        "count": 3,
    }


def test_location_is_redacted_from_messages(tmp_path: Path) -> None:
    rec = events.EventRecorder(tmp_path / "e.jsonl", secrets=[], hidden=["SpringfieldTest"])
    event = rec.to_event(record("[Search] x from springfieldtest, SPRINGFIELDTEST", logging.ERROR))
    assert event is not None
    assert event["message"] == "[Search] x from [location], [location]"


def test_location_is_redacted_from_structured_fields(tmp_path: Path) -> None:
    rec = events.EventRecorder(tmp_path / "e.jsonl", secrets=[], hidden=["springfieldtest"])
    extra = aimm_event("search_summary", item="springfieldtest nuc", new_count=1)
    event = rec.to_event(record("x", **extra))
    assert event is not None and event["item"] == "[location] nuc"


def test_empty_hidden_values_are_ignored(tmp_path: Path) -> None:
    rec = events.EventRecorder(tmp_path / "e.jsonl", secrets=[], hidden=["", "  "])
    event = rec.to_event(record("a b", logging.ERROR))
    assert event is not None and event["message"] == "a b"


def test_attach_passes_hidden_values(tmp_path: Path) -> None:
    logger = logging.getLogger("test-attach-hidden")
    events.attach(logger, tmp_path / "e.jsonl", hidden=["springfieldtest"])
    (handler,) = logger.handlers
    assert isinstance(handler, events.EventRecorder) and handler.hidden == ["springfieldtest"]
    logger.removeHandler(handler)


@pytest.mark.parametrize(
    ("slug", "text", "expected"),
    [
        ("la", "Playwright la failed in la.", "Playwright [location] failed in [location]."),
        ("98101", "id 1298101 and 98101", "id 1298101 and [location]"),
        ("seattle", "/marketplace/Seattle/search?q=x", "/marketplace/[location]/search?q=x"),
        ("seattle", "from seattle, WA", "from [location], WA"),
        ("seattle", "seattleites", "seattleites"),
    ],
)
def test_location_match_respects_word_edges(
    tmp_path: Path, slug: str, text: str, expected: str
) -> None:
    rec = events.EventRecorder(tmp_path / "e.jsonl", secrets=[], hidden=[slug])
    assert rec.redact(text) == expected


WEBHOOK = "https://discord.com/api/webhooks/1234567890/FAKE-token_0123456789abcdef"


@pytest.mark.parametrize("name", ["DISCORD_WEBHOOK_URL", "ALERTS_WEBHOOK", "slack_webhook_url"])
def test_webhook_urls_and_their_tokens_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, WEBHOOK)
    rec = events.EventRecorder(tmp_path / "e.jsonl")
    for text in (f"POST {WEBHOOK} failed", "token FAKE-token_0123456789abcdef alone"):
        event = rec.to_event(record(text, logging.ERROR))
        assert event is not None
        assert "FAKE-token" not in event["message"] and "1234567890/" not in event["message"]
    quoted = rec.to_event(record("GET %2Fapi%2Fwebhooks failed", logging.ERROR))
    assert quoted is not None  # the path alone is not a secret


def test_a_webhook_without_a_token_segment_adds_nothing_odd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("X_WEBHOOK_URL", "abc")
    assert "abc" not in events.secret_values()  # too short to be a useful secret


def test_other_env_names_are_unaffected_by_the_webhook_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBHOOK_DOCS_PATH", "plain-value-here")
    monkeypatch.setenv("MY_WEBHOOK_URL_NOTES", "other-plain-value")
    assert "plain-value-here" not in events.secret_values()
    assert "other-plain-value" not in events.secret_values()


def test_notification_event(recorder: events.EventRecorder) -> None:
    extra = aimm_event("notification", method="ntfy", outcome="failed", title="secret listing")
    event = recorder.to_event(record("[Notify] attempt", **extra))
    assert event == {
        "ts": event["ts"],
        "kind": "notification",
        "method": "ntfy",
        "outcome": "failed",
    }
