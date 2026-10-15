"""Discord deal alerts: a card per listing, posted to one webhook.

aimm has no Discord notifier, so ``launch`` calls ``notify`` from its patched
``NotificationConfig.notify_all``. Everything on a card that came from a seller
or from the AI's reading of their text is untrusted. Discord renders markdown
in embed descriptions and field values (masked links, pings, headings, spoilers),
so each such value is cleaned, defanged and escaped here, and every message sets
``allowed_mentions`` to none as a second line of defence.

``DiscordWebhook.send`` is synchronous inside aimm's notify path (aimm waits
for it), so it is bounded: a 90 s budget for all retries and waits together.

The webhook URL is a bearer credential: it is read from the environment only,
checked by ``validate_webhook_url``, sent to nothing but its own host (redirects
are refused) and never put in a log line or an exception. Messages call it "the
Discord webhook".
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import re
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from deal_hunter.links import cdn_image_url, item_url
from deal_hunter.settings import DISCORD_WEBHOOK_ENV, SettingsError
from deal_hunter.summary import clean_summary

WEBHOOK_ENV = DISCORD_WEBHOOK_ENV

# Discord's limits (documented): 10 embeds and 6,000 embed characters per message,
# 256 characters in a title.
MAX_EMBEDS = 10
MAX_EMBED_CHARS = 6000
TITLE_LIMIT = 256
SUMMARY_LIMIT = 1000
FIELD_LIMIT = 100

TIMEOUT_S = 10.0
MAX_ATTEMPTS = 3
BACKOFF_S = (1.0, 4.0)  # after the 1st and 2nd failed attempts (5xx, network)
MAX_WAIT_S = 60.0  # the longest we wait out a 429
SEND_BUDGET_S = 90.0  # all attempts and waits for one send() together
DEFAULT_WAIT_S = 1.0
_MAX_BODY = 65_536

_WEBHOOK = re.compile(
    r"https://(?:discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)"
    r"/api/webhooks/(?P<id>[0-9]{1,25})/(?P<token>[A-Za-z0-9_-]{1,200})"
)

_ZWSP = "​"
# Characters Discord's markdown gives meaning to; each is backslash-escaped.
_SPECIAL = frozenset("\\*_~`|>#-[]()")
# A scheme or "www." makes Discord turn text into a link; a zero-width space
# after it breaks that and leaves the text readable.
# Any "://" is defanged, whatever scheme or text precedes it: a word character
# before "https" must not hide the link ("xhttps://..." still linkifies).
_SCHEME_SEP = re.compile(r":(?=//)")
_WWW = re.compile(r"www\.", re.IGNORECASE)
# "1. text" at the start of a line is an ordered list in Discord markdown.
_LIST_MARKER = re.compile(r"^([0-9]+)\.(?= )")

_REASONS = {
    "NOT_NOTIFIED": "New listing",
    "LISTING_DISCOUNTED": "Price drop",
    "LISTING_CHANGED": "Updated",
    "EXPIRED": "Reminder",
    "NOTIFIED": "Reminder",  # only reached when aimm forces a re-send
}
_GREEN, _AMBER, _GREY = 0x2ECC71, 0xF1C40F, 0x95A5A6


def validate_webhook_url(url: str) -> str:
    """The webhook URL on discord.com, if url is a Discord webhook; else a SettingsError.

    discordapp.com, ptb.discord.com and canary.discord.com webhooks work on
    discord.com too, so the returned URL always names that one host (the one
    deploy/ allowlists for egress). The error omits the URL.
    """
    url = url.strip()
    matched = _WEBHOOK.fullmatch(url)
    if not matched:
        raise SettingsError(
            f"{WEBHOOK_ENV} is not a Discord webhook URL. Expected "
            "https://discord.com/api/webhooks/<id>/<token> (also discordapp.com, "
            "ptb.discord.com or canary.discord.com). Copy it again from the channel's "
            "Integrations settings."
        )
    return f"https://discord.com/api/webhooks/{matched['id']}/{matched['token']}"


# --- text -------------------------------------------------------------------------


def _units(text: str) -> Iterator[str]:
    """The escaped text, one source character at a time (so a cut never splits an escape)."""
    text = _WWW.sub(f"www{_ZWSP}.", _SCHEME_SEP.sub(f":{_ZWSP}", text))
    marker = _LIST_MARKER.match(text)
    if marker:
        yield marker[1] + "\\."
        text = text[marker.end() :]
    for ch in text:
        if ch in _SPECIAL:
            yield "\\" + ch
        elif ch == "@":
            yield "@" + _ZWSP  # no @everyone, @here or <@id>, even if mentions were allowed
        else:
            yield ch


def escape_markdown(text: str) -> str:
    """text with Discord markdown, links and mentions made inert, so it shows literally."""
    return "".join(_units(text))


def _fit(text: str, limit: int) -> str:
    """Escape text and cut it to at most limit characters, ending in an ellipsis."""
    units = list(_units(text))
    if sum(map(_u16, units)) <= limit:  # Discord counts UTF-16 units, not code points
        return "".join(units)
    out: list[str] = []
    used = 0
    for unit in units:
        if used + _u16(unit) > limit - 1:
            break
        out.append(unit)
        used += _u16(unit)
    return "".join(out).rstrip() + "…"


def _line(value: Any) -> str:
    """One whitespace-collapsed line from whatever aimm gave us, without control characters.

    Control (Cc) and format (Cf) characters go: a bidi override (U+202E) can
    reorder a price on screen, and a zero-width character can hide a link
    break. Only the zero-width spaces ``_units`` adds afterwards remain.
    """
    text = " ".join(str(value if value is not None else "").split())
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))


# --- cards ------------------------------------------------------------------------


def _score(rating: Any) -> int | None:
    try:
        score = int(getattr(rating, "score", None))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 5 else None


def build_embed(listing: Any, rating: Any, status: Any, now: float) -> dict[str, Any]:
    """One listing as a Discord embed. Never carries the description body."""
    score = _score(rating)
    stars = "not rated" if score is None else f"{'★' * score}{'☆' * (5 - score)} {score}/5"

    def field(name: str, value: Any) -> dict[str, Any]:
        return {"name": name, "value": _fit(_line(value), FIELD_LIMIT) or "—", "inline": True}

    card: dict[str, Any] = {
        "title": _fit(_line(getattr(listing, "title", "")), TITLE_LIMIT) or "(untitled)",
        "color": _GREY if score is None or score < 3 else _AMBER if score == 3 else _GREEN,
        "fields": [
            field("Price", getattr(listing, "price", "")),
            {"name": "Rating", "value": stars, "inline": True},
            field("Condition", getattr(listing, "condition", "")),
            field("Location", getattr(listing, "location", "")),
            field("Watch", getattr(listing, "name", "")),
        ],
        "footer": {"text": _REASONS.get(getattr(status, "name", ""), "Reminder")},
    }
    url = item_url(_line(getattr(listing, "id", "")))
    if url:
        card["url"] = url
    image = cdn_image_url(str(getattr(listing, "image", "") or ""), now)
    if image:
        card["thumbnail"] = {"url": image}
    # Older cached ratings predate the cleaning in launch.py; cleaning is idempotent.
    # Escape only after cleaning: escaping first would hand the cleaner backslashes.
    summary = _line(clean_summary(str(getattr(rating, "comment", "") or "")))
    if summary:
        card["description"] = _fit(summary, SUMMARY_LIMIT)
    return card


def embed_chars(embed: dict[str, Any]) -> int:
    """The UTF-16 units Discord counts toward a message's 6,000: text fields only."""
    total = _u16(embed.get("title", "")) + _u16(embed.get("description", ""))
    total += _u16(embed.get("footer", {}).get("text", ""))
    total += sum(_u16(f["name"]) + _u16(f["value"]) for f in embed.get("fields", []))
    return total


def _u16(text: str) -> int:
    """Length in UTF-16 code units, which is how Discord counts (an emoji is 2)."""
    return len(text.encode("utf-16-le")) // 2


def _message(embeds: list[dict[str, Any]]) -> dict[str, Any]:
    return {"embeds": embeds, "allowed_mentions": {"parse": []}}


def build_messages(embeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages of at most MAX_EMBEDS embeds and MAX_EMBED_CHARS characters, in order."""
    messages: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []
    used = 0
    for e in embeds:
        size = embed_chars(e)
        if batch and (len(batch) >= MAX_EMBEDS or used + size > MAX_EMBED_CHARS):
            messages.append(_message(batch))
            batch, used = [], 0
        batch.append(e)
        used += size
    if batch:
        messages.append(_message(batch))
    return messages


def sample_message() -> dict[str, Any]:
    """The `notify-test` card: a fixed fake listing, no photo."""
    fake = {
        "title": "deal-hunter test notification",
        "price": "$100",
        "condition": "Test",
        "location": "Nowhere",
        "name": "test watch",
        "id": "",
        "image": "",
    }
    rating = SimpleNamespace(score=4, comment="This is only a test; no real listing.")
    status = SimpleNamespace(name="NOT_NOTIFIED")
    return _message([build_embed(SimpleNamespace(**fake), rating, status, time.time())])


# --- sending ----------------------------------------------------------------------


@dataclass
class Reply:
    status: int
    body: bytes = b""
    retry_after: str | None = None  # the Retry-After header, if any


Transport = Callable[[str, bytes, float], Reply]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the webhook URL (and the card) go to one host only."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(url: str, body: bytes, timeout: float) -> Reply:
    """POST body as JSON. Any HTTP answer (redirects too) is a Reply; none raises OSError."""
    if not url.startswith(("https://", "http://")):
        raise ValueError("webhook transport only speaks http(s)")
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "deal-hunter"},
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:  # noqa: S310
            return Reply(response.status, response.read(_MAX_BODY))
    except urllib.error.HTTPError as e:
        try:
            return Reply(e.code, e.read(_MAX_BODY), e.headers.get("Retry-After"))
        finally:
            e.close()


def _wait_for(reply: Reply) -> float:
    """How long a 429 asks us to wait, bounded to 0..MAX_WAIT_S."""
    candidate: Any = None
    try:
        parsed = json.loads(reply.body)
        if isinstance(parsed, dict):
            candidate = parsed.get("retry_after")
    except ValueError:
        pass
    if candidate is None:
        candidate = reply.retry_after
    try:
        seconds = float(candidate)
    except (TypeError, ValueError):
        return DEFAULT_WAIT_S
    if math.isnan(seconds):
        return DEFAULT_WAIT_S
    return min(max(seconds, 0.0), MAX_WAIT_S)


def _backoff(attempt: int) -> float | None:
    """The pause after a failed attempt (1-based), or None when there is no attempt left."""
    return BACKOFF_S[attempt - 1] if attempt <= len(BACKOFF_S) else None


class DiscordWebhook:
    """Posts messages to one webhook. The transport and sleep are injectable for tests."""

    def __init__(
        self,
        url: str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = TIMEOUT_S,
        logger: logging.Logger | None = None,
    ) -> None:
        self._url = url  # never logged, never in an exception
        self.transport = transport or urllib_transport  # looked up now, so tests can swap it
        self.last_error: str | None = None  # why the latest send failed; never the URL
        self.sleep = sleep
        self.clock = clock
        self._deadline = 0.0
        self.timeout = timeout
        self.logger = logger or logging.getLogger("monitor")

    def __repr__(self) -> str:
        return "DiscordWebhook(<redacted>)"

    def send(self, messages: list[dict[str, Any]]) -> bool:
        """True if Discord accepted at least one card and nothing failed outright.

        This runs synchronously inside aimm's notify path, so everything is
        bounded: SEND_BUDGET_S for all attempts and waits together. A message
        Discord rejects with 400 is sent again one card per message; a single
        card it still rejects is dropped with an error log (last_error says so),
        so one malformed card can't block the others; if none is accepted the
        result is False and aimm tries the listings again next cycle. Any other
        failure (a bad webhook, an outage, the budget) stops the send and returns False.
        """
        self.last_error = None
        self._deadline = self.clock() + SEND_BUDGET_S
        if not messages:
            return False
        accepted = 0
        for message in messages:
            outcome = self._post(message)
            if outcome == "ok":
                accepted += len(message["embeds"])
            elif outcome == "failed":
                return False  # the rest would only be sent twice next cycle
            else:  # rejected (HTTP 400): try the cards one by one
                for card in message["embeds"]:
                    if len(message["embeds"]) == 1:
                        outcome = "rejected"
                    else:
                        outcome = self._post({**message, "embeds": [card]})
                    if outcome == "ok":
                        accepted += 1
                    elif outcome == "failed":
                        return False
                    else:
                        self._dropped(accepted > 0)
        # A rejection is not a delivery: with nothing accepted, aimm must not mark
        # the listings notified (a forum-channel webhook rejects every card).
        return accepted > 0

    def _dropped(self, others_sent: bool) -> None:
        self.last_error = "HTTP 400 (card rejected)"
        self.logger.error(
            "[Discord] The Discord webhook rejected a card (HTTP 400); skipped it."
            + (" Other cards were sent." if others_sent else "")
        )

    def _post(self, message: dict[str, Any]) -> str:
        """ "ok", "rejected" (HTTP 400, not logged here) or "failed" (logged)."""
        body = json.dumps(message).encode()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait: float | None
            try:
                reply = self.transport(self._url, body, self.timeout)
            except (OSError, http.client.HTTPException, ValueError) as e:
                # The type only: library messages can quote the URL.
                problem, wait = f"network error ({type(e).__name__})", _backoff(attempt)
            else:
                if 200 <= reply.status < 300:
                    return "ok"
                if reply.status == 429:
                    problem, wait = "HTTP 429 (rate limited)", _wait_for(reply)
                elif reply.status >= 500:
                    problem, wait = f"HTTP {reply.status}", _backoff(attempt)
                elif reply.status == 400:
                    return "rejected"
                else:
                    self._fail(f"HTTP {reply.status}{self._hint(reply.status)}")
                    return "failed"
            if attempt == MAX_ATTEMPTS or wait is None:
                self._fail(f"{problem}; gave up after {attempt} attempts")
                return "failed"
            if self.clock() + wait > self._deadline:
                self._fail(f"{problem}; out of time ({SEND_BUDGET_S:g} s) before the next try")
                return "failed"
            self.logger.warning(
                f"[Discord] The Discord webhook: {problem}; trying again in {wait:g} s."
            )
            self.sleep(wait)
        return "failed"  # pragma: no cover — the loop always returns

    @staticmethod
    def _hint(status: int) -> str:
        if status == 404:
            return " (webhook deleted or wrong)"
        if status in (301, 302, 303, 307, 308):
            return " (redirect refused)"
        if status in (401, 403):
            return " (webhook token rejected)"
        return ""

    def _fail(self, what: str) -> None:
        self.last_error = what
        self.logger.error(f"[Discord] Couldn't post to the Discord webhook: {what}")


def notify(
    webhook: DiscordWebhook,
    listings: list[Any],
    ratings: list[Any],
    statuses: list[Any],
    force: bool = False,
) -> bool:
    """Post a card for each listing aimm hasn't already notified (all of them if force)."""
    now = time.time()
    embeds = []
    for listing, rating, status in zip(listings, ratings, statuses, strict=False):
        if not force and getattr(status, "name", "") == "NOTIFIED":
            continue
        try:
            embeds.append(build_embed(listing, rating, status, now))
        except Exception as e:  # noqa: BLE001 — one odd listing must not block the others
            webhook.logger.error(
                f"[Discord] Skipped a listing that couldn't be built: {type(e).__name__}"
            )
    return webhook.send(build_messages(embeds))
