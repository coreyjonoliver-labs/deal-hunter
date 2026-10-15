"""Discord cards: payloads, escaping, batching, and the sender against a local fake webhook."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from ai_marketplace_monitor.notification import NotificationStatus as NS

from deal_hunter import discord
from deal_hunter.discord import (
    DiscordWebhook,
    build_embed,
    build_messages,
    embed_chars,
    escape_markdown,
    validate_webhook_url,
)
from deal_hunter.settings import SettingsError

FAKE_ID = "1234567890"
FAKE_TOKEN = "FAKE-token_0123456789abcdef"  # noqa: S105 — a fake, for redaction checks
FAKE_URL = f"https://discord.com/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}"
NOW = 1_700_000_000.0
GOOD_IMAGE = "https://scontent.xx.fbcdn.net/v/t45/photo.jpg?oe=7FFFFFFF&oh=abc"


def listing(**over: Any) -> Any:
    fields: dict[str, Any] = {
        "marketplace": "facebook",
        "name": "standing desk",
        "id": "123456",
        "title": "Uplift V2 standing desk",
        "image": GOOD_IMAGE,
        "price": "$220",
        "post_url": "https://www.facebook.com/marketplace/item/123456/?ref=search",
        "location": "Springfield, IL",
        "seller": "someone",
        "condition": "Used - Good",
        "description": "SECRET-DESCRIPTION-BODY light wear",
    }
    return SimpleNamespace(**(fields | over))


def rating(score: int = 4, comment: str = "Fair ~$350 vs $220 ask; buy") -> Any:
    return SimpleNamespace(score=score, comment=comment, name="standing desk")


def embed(**over: Any) -> dict[str, Any]:
    return build_embed(listing(**over), rating(), NS.NOT_NOTIFIED, NOW)


def fields_of(e: dict[str, Any]) -> dict[str, str]:
    return {f["name"]: f["value"] for f in e["fields"]}


# --- URL validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"]
)
def test_valid_webhook_hosts(host: str) -> None:
    # Every accepted host is rewritten to discord.com (webhooks work there, and it is
    # the one host the egress allowlist names).
    url = f"https://{host}/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}"
    assert validate_webhook_url(url) == FAKE_URL


def test_webhook_url_is_trimmed() -> None:
    assert validate_webhook_url(f"  {FAKE_URL}\n") == FAKE_URL


@pytest.mark.parametrize(
    "url",
    [
        f"https://example.com/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}",
        f"http://discord.com/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}",
        f"https://discord.com.evil.example/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}",
        f"https://evil.example/discord.com/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}",
        f"https://user@discord.com/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}",
        f"https://discord.com:8443/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}",
        f"https://discord.com/api/webhooks/abc/{FAKE_TOKEN}",
        f"https://discord.com/api/webhooks/{FAKE_ID}/",
        f"https://discord.com/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}/extra",
        f"https://discord.com/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}?wait=true",
        f"https://discord.com/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}#x",
        f"https://discord.com/api/v10/webhooks/{FAKE_ID}/{FAKE_TOKEN}",
        f"https://discord.com\\@evil.example/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}",
        "",
        "not a url",
    ],
)
def test_bad_webhook_urls_do_not_echo_the_url(url: str) -> None:
    with pytest.raises(SettingsError) as raised:
        validate_webhook_url(url)
    message = str(raised.value)
    assert "DISCORD_WEBHOOK_URL" in message
    assert FAKE_TOKEN not in message and "evil" not in message and "example.com" not in message


# --- escaping ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("plain text 123", "plain text 123"),
        ("**bold**", r"\*\*bold\*\*"),
        ("_i_ ~~s~~ `c` ||spoiler||", r"\_i\_ \~\~s\~\~ \`c\` \|\|spoiler\|\|"),
        ("> quote # head - list", r"\> quote \# head \- list"),
        ("[click](https://x)", "\\[click\\]\\(https:\u200b//x\\)"),
        ("back\\slash", "back\\\\slash"),
        ("@everyone @here", "@\u200beveryone @\u200bhere"),
        ("a <@123> b", "a <@\u200b123\\> b"),
        ("see www.evil.example now", "see www\u200b.evil.example now"),
        ("HTTP://EVIL.EXAMPLE", "HTTP:\u200b//EVIL.EXAMPLE"),
    ],
)
def test_escape_table(raw: str, escaped: str) -> None:
    assert escape_markdown(raw) == escaped


def test_hostile_title_is_inert() -> None:
    e = embed(title="@everyone FREE [click](https://evil.example) **NOW**")
    assert e["title"] == (
        "@\u200beveryone FREE \\[click\\]\\(https:\u200b//evil.example\\) \\*\\*NOW\\*\\*"
    )
    message = build_messages([e])[0]
    assert message["allowed_mentions"] == {"parse": []}


def test_titles_of_only_markdown_stay_visible() -> None:
    assert embed(title="***")["title"] == r"\*\*\*"
    assert embed(title="")["title"] == "(untitled)"
    assert embed(title="   ")["title"] == "(untitled)"


def test_summary_is_cleaned_then_escaped() -> None:
    # clean_summary drops the stray ** pair; escaping afterwards keeps the rest literal.
    e = build_embed(
        listing(), rating(comment="**Fair** [x](https://evil.example) ~$350"), NS.NOT_NOTIFIED, NOW
    )
    assert "**" not in e["description"].replace("\\*", "")
    assert e["description"].startswith("Fair ")
    assert "\\[x\\]\\(" in e["description"]
    assert "\\~$350" in e["description"]


def test_summary_escaping_is_not_cleaned_away() -> None:
    # Cleaning after escaping would eat the backslashes' targets; the order matters.
    e = build_embed(listing(), rating(comment="a_b and 2*3"), NS.NOT_NOTIFIED, NOW)
    assert e["description"] == r"a\_b and 2\*3"


# --- card contents ----------------------------------------------------------------


def test_card_fields() -> None:
    e = embed()
    assert e["title"] == "Uplift V2 standing desk"
    assert e["url"] == "https://www.facebook.com/marketplace/item/123456/"
    assert e["thumbnail"] == {"url": GOOD_IMAGE}
    assert e["description"] == "Fair ~$350 vs $220 ask; buy".replace("~", "\\~")
    assert e["footer"] == {"text": "New listing"}
    assert fields_of(e) == {
        "Price": "$220",
        "Rating": "★★★★☆ 4/5",
        "Condition": "Used \\- Good",
        "Location": "Springfield, IL",
        "Watch": "standing desk",
    }


def test_card_has_no_description_body_and_only_two_urls() -> None:
    text = json.dumps(embed(description="https://evil.example/body"))
    assert "SECRET-DESCRIPTION-BODY" not in text and "evil.example" not in text
    assert text.count("https://") == 2  # the item page and the photo


@pytest.mark.parametrize(
    ("image", "shown"),
    [
        (GOOD_IMAGE, True),
        ("", False),
        ("http://scontent.xx.fbcdn.net/p.jpg", False),
        ("https://evil.example/p.jpg", False),
        ("https://fbcdn.net.evil.example/p.jpg", False),
        ("https://scontent.xx.fbcdn.net/p.jpg?oe=1", False),  # expired
    ],
)
def test_thumbnail_allowlist(image: str, shown: bool) -> None:
    assert ("thumbnail" in embed(image=image)) is shown


@pytest.mark.parametrize("item_id", ["", "abc", "12/../x", "1 2", "1" * 30])
def test_no_item_url_without_a_numeric_id(item_id: str) -> None:
    assert "url" not in embed(id=item_id)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (NS.NOT_NOTIFIED, "New listing"),
        (NS.LISTING_DISCOUNTED, "Price drop"),
        (NS.LISTING_CHANGED, "Updated"),
        (NS.EXPIRED, "Reminder"),
        (NS.NOTIFIED, "Reminder"),  # only with force
    ],
)
def test_reason_from_status(status: NS, reason: str) -> None:
    assert build_embed(listing(), rating(), status, NOW)["footer"] == {"text": reason}


def test_missing_values_still_make_a_card() -> None:
    e = build_embed(listing(price="", condition=None, location=""), rating(), NS.NOT_NOTIFIED, NOW)
    assert all(v for v in fields_of(e).values())


def test_unrated_listing_is_still_a_card() -> None:
    e = build_embed(listing(), None, NS.NOT_NOTIFIED, NOW)
    assert fields_of(e)["Rating"] == "not rated" and "description" not in e


def test_limits() -> None:
    e = embed(title="t" * 400, location="l" * 5000, condition="c" * 5000)
    e2 = build_embed(listing(), rating(comment="word " * 600), NS.NOT_NOTIFIED, NOW)
    assert len(e["title"]) <= 256
    assert all(len(v) <= 100 for v in fields_of(e).values())
    assert len(e2["description"]) <= 1000


def test_truncation_never_splits_an_escape() -> None:
    e = embed(title="*" * 400)
    assert len(e["title"]) <= 256 and e["title"].endswith("…")
    body = e["title"][:-1]
    assert body == "\\*" * (len(body) // 2)


# --- batching ---------------------------------------------------------------------


def embeds(n: int, summary: str = "ok") -> list[dict[str, Any]]:
    return [
        build_embed(listing(id=str(i)), rating(comment=summary), NS.NOT_NOTIFIED, NOW)
        for i in range(n)
    ]


def test_23_listings_make_three_messages() -> None:
    messages = build_messages(embeds(23))
    assert [len(m["embeds"]) for m in messages] == [10, 10, 3]
    assert all(m["allowed_mentions"] == {"parse": []} for m in messages)
    assert all(sum(embed_chars(e) for e in m["embeds"]) < 6000 for m in messages)


def test_batches_split_on_characters() -> None:
    big = [{"title": "t" * 256, "description": "x" * 1000} for _ in range(12)]
    messages = build_messages(big)
    assert all(sum(embed_chars(e) for e in m["embeds"]) <= 6000 for m in messages)
    assert [len(m["embeds"]) for m in messages] == [4, 4, 4]
    assert sum(len(m["embeds"]) for m in messages) == 12


def test_no_embeds_no_messages() -> None:
    assert build_messages([]) == []


# --- the sender, against a local fake webhook ---------------------------------------


@dataclass
class Seen:
    path: str
    body: bytes
    content_type: str


class FakeWebhook:
    """A local webhook that answers each POST from a script, then 204."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], bytes, float]] = []
        self.seen: list[Seen] = []
        self.other_hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path.startswith("/elsewhere"):
                    outer.other_hits += 1
                else:
                    outer.seen.append(Seen(self.path, body, self.headers.get("Content-Type", "")))
                status, headers, payload, delay = (
                    outer.script.pop(0) if outer.script else (204, {}, b"", 0)
                )
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:  # the client gave up (a timeout test)
                    pass

            do_GET = do_POST  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}"
        self.base = f"http://127.0.0.1:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hook() -> Iterator[FakeWebhook]:
    fake = FakeWebhook()
    yield fake
    fake.close()


@pytest.fixture
def sleeps() -> list[float]:
    return []


def sender(hook: FakeWebhook, sleeps: list[float], **kw: Any) -> DiscordWebhook:
    return DiscordWebhook(hook.url, sleep=sleeps.append, **kw)


def one_message(n: int = 1) -> list[dict[str, Any]]:
    return build_messages(embeds(n))


def test_posts_json_and_succeeds(hook: FakeWebhook, sleeps: list[float]) -> None:
    messages = one_message()
    assert sender(hook, sleeps).send(messages) is True
    assert len(hook.seen) == 1 and sleeps == []
    assert hook.seen[0].content_type == "application/json"
    assert hook.seen[0].path == f"/api/webhooks/{FAKE_ID}/{FAKE_TOKEN}"
    assert json.loads(hook.seen[0].body) == messages[0]


def test_nothing_to_send_is_not_a_success(hook: FakeWebhook, sleeps: list[float]) -> None:
    assert sender(hook, sleeps).send([]) is False
    assert hook.seen == []


def test_429_waits_retry_after_then_succeeds(hook: FakeWebhook, sleeps: list[float]) -> None:
    hook.script = [(429, {}, b'{"message": "slow down", "retry_after": 2}', 0)]
    assert sender(hook, sleeps).send(one_message()) is True
    assert sleeps == [2.0] and len(hook.seen) == 2


def test_429_wait_is_capped_and_attempts_are_limited(
    hook: FakeWebhook, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    hook.script = [(429, {}, b'{"retry_after": 3600}', 0)] * 3
    with caplog.at_level(logging.ERROR):
        assert sender(hook, sleeps, logger=logging.getLogger("t")).send(one_message()) is False
    assert len(hook.seen) == 3
    assert sleeps == [60.0, 60.0]  # no wait after the last attempt
    assert "429" in caplog.text


def test_429_without_a_usable_retry_after(hook: FakeWebhook, sleeps: list[float]) -> None:
    hook.script = [(429, {"Retry-After": "3"}, b"not json", 0), (429, {}, b"[]", 0)]
    assert sender(hook, sleeps).send(one_message()) is True
    assert sleeps == [3.0, 1.0]


@pytest.mark.parametrize("bad", ["-5", "nan", "inf", "x"])
def test_odd_retry_after_values_are_bounded(
    hook: FakeWebhook, sleeps: list[float], bad: str
) -> None:
    hook.script = [(429, {}, json.dumps({"retry_after": bad}).encode(), 0)]
    assert sender(hook, sleeps).send(one_message()) is True
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= 60


def test_5xx_retries_with_backoff(hook: FakeWebhook, sleeps: list[float]) -> None:
    hook.script = [(502, {}, b"", 0), (503, {}, b"", 0)]
    assert sender(hook, sleeps).send(one_message()) is True
    assert sleeps == [1, 4] and len(hook.seen) == 3


def test_5xx_gives_up_after_three_attempts(hook: FakeWebhook, sleeps: list[float]) -> None:
    hook.script = [(500, {}, b"", 0)] * 5
    assert sender(hook, sleeps).send(one_message()) is False
    assert sleeps == [1, 4] and len(hook.seen) == 3


def test_network_errors_retry_and_never_log_the_url(sleeps: list[float], caplog: Any) -> None:
    calls: list[str] = []

    def broken(url: str, body: bytes, timeout: float) -> discord.Reply:
        calls.append(url)
        raise ConnectionResetError(f"reset talking to {url}")

    web = DiscordWebhook(
        FAKE_URL, transport=broken, sleep=sleeps.append, logger=logging.getLogger("t")
    )
    with caplog.at_level(logging.DEBUG):
        assert web.send(one_message()) is False
    assert len(calls) == 3 and sleeps == [1, 4]
    assert FAKE_TOKEN not in caplog.text and FAKE_ID not in caplog.text
    assert "ConnectionResetError" in caplog.text


def test_a_timeout_is_a_retried_network_error(hook: FakeWebhook, sleeps: list[float]) -> None:
    hook.script = [(204, {}, b"", 1.0)]
    assert sender(hook, sleeps, timeout=0.2).send(one_message()) is True
    assert sleeps == [1] and len(hook.seen) == 2


def test_default_timeout_is_ten_seconds() -> None:
    assert DiscordWebhook(FAKE_URL).timeout == 10


@pytest.mark.parametrize(
    ("status", "hint"),
    [(404, "webhook deleted or wrong"), (401, "401"), (403, "403")],
)
def test_other_4xx_is_not_retried_and_logs_no_secrets(
    hook: FakeWebhook, sleeps: list[float], caplog: Any, status: int, hint: str
) -> None:
    hook.script = [(status, {}, b'{"message": "Unknown Webhook"}', 0)]
    web = sender(hook, sleeps, logger=logging.getLogger("t"))
    with caplog.at_level(logging.DEBUG):
        assert web.send(one_message()) is False
    assert len(hook.seen) == 1 and sleeps == []
    assert str(status) in caplog.text and hint in caplog.text
    assert "the Discord webhook" in caplog.text
    assert FAKE_TOKEN not in caplog.text and hook.base not in caplog.text


def test_redirects_are_refused(hook: FakeWebhook, sleeps: list[float], caplog: Any) -> None:
    hook.script = [(307, {"Location": f"{hook.base}/elsewhere"}, b"", 0)]
    web = sender(hook, sleeps, logger=logging.getLogger("t"))
    with caplog.at_level(logging.DEBUG):
        assert web.send(one_message()) is False
    assert hook.other_hits == 0 and len(hook.seen) == 1 and sleeps == []
    assert "307" in caplog.text and "elsewhere" not in caplog.text


def test_a_failed_batch_stops_the_rest(hook: FakeWebhook, sleeps: list[float]) -> None:
    hook.script = [(204, {}, b"", 0), (404, {}, b"", 0)]
    assert sender(hook, sleeps).send(one_message(23)) is False
    assert len(hook.seen) == 2  # batch 3 is not attempted


def test_three_batches_are_three_posts(hook: FakeWebhook, sleeps: list[float]) -> None:
    assert sender(hook, sleeps).send(one_message(23)) is True
    assert [len(json.loads(s.body)["embeds"]) for s in hook.seen] == [10, 10, 3]


# --- deciding what to send -----------------------------------------------------------


def test_notify_skips_already_notified_unless_forced(
    hook: FakeWebhook, sleeps: list[float]
) -> None:
    web = sender(hook, sleeps)
    listings = [listing(id="1"), listing(id="2"), listing(id="3")]
    ratings = [rating(), rating(), rating()]
    statuses = [NS.NOT_NOTIFIED, NS.NOTIFIED, NS.LISTING_DISCOUNTED]
    assert discord.notify(web, listings, ratings, statuses, force=False) is True
    titles = [e["url"] for e in json.loads(hook.seen[0].body)["embeds"]]
    assert titles == [
        "https://www.facebook.com/marketplace/item/1/",
        "https://www.facebook.com/marketplace/item/3/",
    ]
    hook.seen.clear()
    assert discord.notify(web, listings, ratings, statuses, force=True) is True
    assert len(json.loads(hook.seen[0].body)["embeds"]) == 3


def test_notify_with_nothing_new_posts_nothing(hook: FakeWebhook, sleeps: list[float]) -> None:
    web = sender(hook, sleeps)
    assert discord.notify(web, [listing()], [rating()], [NS.NOTIFIED], force=False) is False
    assert hook.seen == []


def test_a_listing_with_no_attributes_still_makes_a_card(
    hook: FakeWebhook, sleeps: list[float]
) -> None:
    web = sender(hook, sleeps)
    assert discord.notify(web, [SimpleNamespace()], [rating()], [NS.NOT_NOTIFIED]) is True
    assert json.loads(hook.seen[0].body)["embeds"][0]["title"] == "(untitled)"


# --- the test card -------------------------------------------------------------------


def test_sample_card_is_labelled_and_needs_no_photo() -> None:
    message = discord.sample_message()
    e = message["embeds"][0]
    assert e["title"] == "deal\\-hunter test notification"
    assert "thumbnail" not in e and "url" not in e
    assert message["allowed_mentions"] == {"parse": []}


# --- review round 1 ------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["x", "1", "_", "é", "日", "-", "a.b", "", "(", "\\"])
@pytest.mark.parametrize("scheme", ["https", "HTTP", "ftp", "steam", "discord", "x+y.z"])
def test_every_scheme_separator_is_defanged_whatever_precedes_it(prefix: str, scheme: str) -> None:
    out = escape_markdown(f"{prefix}{scheme}://evil.example/p")
    assert "://" not in out
    assert f"{scheme}:\u200b//evil.example" in out


def test_defanged_title_has_no_link_in_a_card() -> None:
    e = embed(title="xhttps://evil.example 1https://e.co _https://e.co éhttps://e.co")
    assert "://" not in e["title"] and "://" not in json.dumps(e["fields"])


@pytest.mark.parametrize("prefix", ["x", "1", "é"])
def test_www_is_defanged_whatever_precedes_it(prefix: str) -> None:
    assert "www." not in escape_markdown(f"{prefix}www.evil.example")


def test_summary_is_one_line_so_no_list_or_heading_starts() -> None:
    e = build_embed(listing(), rating(comment="Fair.\n1. buy now\n- more"), NS.NOT_NOTIFIED, NOW)
    assert "\n" not in e["description"]


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("1. buy", "1\\. buy"),
        ("12) buy", "12\\) buy"),
        ("a 1. b", "a 1. b"),
        ("3.5 mm", "3.5 mm"),
    ],
)
def test_a_leading_ordered_list_marker_is_escaped(raw: str, escaped: str) -> None:
    assert escape_markdown(raw) == escaped


@pytest.mark.parametrize(
    "bad", ["\u202e", "\u202d", "\u2066", "\u200b", "\u200f", "\x00", "\x1b", "\ufeff"]
)
def test_control_and_format_characters_are_stripped_from_seller_text(bad: str) -> None:
    e = embed(title=f"A{bad}B", price=f"${bad}220", location=f"S{bad}pringfield")
    assert e["title"] == "AB"
    values = fields_of(e)
    assert values["Price"] == "$220" and values["Location"] == "Springfield"
    d = build_embed(listing(), rating(comment=f"Fair{bad}price"), NS.NOT_NOTIFIED, NOW)
    assert d["description"] == "Fairprice"


def test_our_own_zero_width_spaces_survive() -> None:
    assert "\u200b" in embed(title="@everyone")["title"]


def test_embed_characters_count_utf16_code_units() -> None:
    assert embed_chars({"title": "😀" * 10}) == 20
    assert embed_chars({"title": "é" * 10}) == 10


def test_batching_counts_emoji_as_two() -> None:
    # Emoji are 2 UTF-16 units: the last case is 6,120 units for two cards, but only
    # 3,060 code points, which the old count would have sent in one message.
    cards = [{"title": "😀" * 120, "description": "😀" * 1370} for _ in range(2)]
    assert embed_chars(cards[0]) == 2980
    assert [len(m["embeds"]) for m in build_messages(cards * 1)] == [2]
    cards = [{"title": "😀" * 130, "description": "😀" * 1370} for _ in range(2)]
    assert embed_chars(cards[0]) == 3000
    assert [len(m["embeds"]) for m in build_messages(cards)] == [2]
    cards = [{"title": "😀" * 130, "description": "😀" * 1400} for _ in range(2)]
    assert embed_chars(cards[0]) == 3060
    assert [len(m["embeds"]) for m in build_messages(cards)] == [1, 1]


def test_webhook_url_digits_are_ascii_only() -> None:
    with pytest.raises(SettingsError):
        validate_webhook_url(f"https://discord.com/api/webhooks/\u0661\u0662\u0663/{FAKE_TOKEN}")


def test_a_trailing_newline_inside_the_url_is_rejected() -> None:
    with pytest.raises(SettingsError):
        validate_webhook_url(f"https://discord.com/api/webhooks/1/{FAKE_TOKEN}\n/x")


def test_a_listing_that_breaks_the_card_builder_is_skipped(
    hook: FakeWebhook, sleeps: list[float], caplog: Any
) -> None:
    class Hostile:
        @property
        def title(self) -> str:
            raise RuntimeError("secret detail https://x.example")

    web = sender(hook, sleeps, logger=logging.getLogger("t"))
    with caplog.at_level(logging.DEBUG):
        ok = discord.notify(
            web, [Hostile(), listing(id="9")], [rating()] * 2, [NS.NOT_NOTIFIED] * 2
        )
    assert ok is True  # the good card was delivered
    assert len(json.loads(hook.seen[0].body)["embeds"]) == 1
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


def test_a_400_on_a_multi_embed_message_retries_one_card_per_message(
    hook: FakeWebhook, sleeps: list[float]
) -> None:
    hook.script = [(400, {}, b"{}", 0)]
    assert sender(hook, sleeps).send(one_message(3)) is True
    sizes = [len(json.loads(s.body)["embeds"]) for s in hook.seen]
    assert sizes == [3, 1, 1, 1] and sleeps == []


def test_one_malformed_card_is_dropped_without_failing_the_batch(
    hook: FakeWebhook, sleeps: list[float], caplog: Any
) -> None:
    hook.script = [(400, {}, b"{}", 0), (204, {}, b"", 0), (400, {}, b"{}", 0), (204, {}, b"", 0)]
    web = sender(hook, sleeps, logger=logging.getLogger("t"))
    with caplog.at_level(logging.DEBUG):
        assert web.send(one_message(3)) is True
    assert len(hook.seen) == 4  # whole, card 1 ok, card 2 rejected, card 3 ok
    assert "400" in caplog.text and FAKE_TOKEN not in caplog.text


def test_a_rejected_card_is_not_a_delivery(
    hook: FakeWebhook, sleeps: list[float], caplog: Any
) -> None:
    hook.script = [(400, {}, b"{}", 0)]
    web = sender(hook, sleeps, logger=logging.getLogger("t"))
    with caplog.at_level(logging.DEBUG):
        assert web.send(one_message(1)) is False
    assert len(hook.seen) == 1 and sleeps == []
    assert web.last_error is not None and "400" in web.last_error
    assert FAKE_TOKEN not in web.last_error and "Other cards were sent" not in caplog.text
    assert "400" in caplog.text


def test_a_webhook_that_rejects_everything_never_succeeds(sleeps: list[float]) -> None:
    def always_400(url: str, body: bytes, timeout: float) -> discord.Reply:
        return discord.Reply(400)

    web = DiscordWebhook(FAKE_URL, transport=always_400, sleep=sleeps.append)
    assert web.send([discord.sample_message()]) is False
    assert web.send(one_message(23)) is False
    assert web.last_error is not None and "400" in web.last_error


def test_mixed_batch_delivers_the_rest_and_reports_the_drop(
    hook: FakeWebhook, sleeps: list[float], caplog: Any
) -> None:
    hook.script = [(400, {}, b"{}", 0), (204, {}, b"", 0), (400, {}, b"{}", 0), (204, {}, b"", 0)]
    web = sender(hook, sleeps, logger=logging.getLogger("t"))
    with caplog.at_level(logging.DEBUG):
        assert web.send(one_message(3)) is True
    assert len(hook.seen) == 4
    assert "Other cards were sent" in caplog.text
    assert web.last_error is not None and "400" in web.last_error


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_bad_webhook_still_fails_the_batch(
    hook: FakeWebhook, sleeps: list[float], status: int
) -> None:
    hook.script = [(status, {}, b"{}", 0)]
    assert sender(hook, sleeps).send(one_message(3)) is False
    assert len(hook.seen) == 1


def test_split_cards_that_fail_for_other_reasons_fail_the_batch(
    hook: FakeWebhook, sleeps: list[float]
) -> None:
    hook.script = [(400, {}, b"{}", 0), (404, {}, b"{}", 0)]
    assert sender(hook, sleeps).send(one_message(3)) is False
    assert len(hook.seen) == 2


def test_send_has_a_total_time_budget(hook: FakeWebhook) -> None:
    now = [1000.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    hook.script = [(429, {}, b'{"retry_after": 60}', 0)] * 5
    web = DiscordWebhook(hook.url, sleep=sleep, clock=lambda: now[0])
    assert web.send(one_message()) is False
    assert waits == [60.0] and len(hook.seen) == 2  # a second 60 s wait would pass 90 s
    assert web.last_error is not None and "time" in web.last_error


def test_the_time_budget_is_per_send(hook: FakeWebhook) -> None:
    now = [0.0]
    web = DiscordWebhook(hook.url, sleep=lambda s: None, clock=lambda: now[0])
    assert web.send(one_message()) is True
    now[0] += 500
    assert web.send(one_message()) is True


def test_long_astral_text_is_cut_by_utf16_units() -> None:
    e = embed(title="😀" * 200, location="😀" * 100)
    assert len(e["title"].encode("utf-16-le")) // 2 <= 256 and e["title"].endswith("…")
    assert len(fields_of(e)["Location"].encode("utf-16-le")) // 2 <= 100
    e2 = build_embed(listing(), rating(comment="😀" * 200), NS.NOT_NOTIFIED, NOW)
    assert len(e2["description"].encode("utf-16-le")) // 2 <= 1000


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("12.5 GB", "12.5 GB"),
        ("3.5mm", "3.5mm"),
        ("1.", "1."),
        ("1. buy", "1\\. buy"),
        ("12. a", "12\\. a"),
    ],
)
def test_only_a_real_list_marker_is_escaped(raw: str, escaped: str) -> None:
    assert escape_markdown(raw) == escaped
