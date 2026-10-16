"""Start ai-marketplace-monitor with fixes applied to the pinned release.

1. URLs are hidden from its keyword filters. aimm's keyword and antikeyword
   filters are a plain substring test over title + description, so "nuc"
   matches inside a link like ``...?srsltid=...RLqNuc`` and an unrelated
   listing goes on to the AI.
2. Page loads time out. aimm navigates with ``page.goto(url, timeout=0)``,
   which waits forever: if the machine sleeps or the network drops mid-load,
   the monitor hangs silently. With a timeout, aimm's own retry loop runs; a
   page that still fails is skipped (listing pages) or stops the monitor with
   an error (search pages), so a supervisor can restart it.
3. The Facebook login is reused. aimm starts a fresh browser with no cookies
   on every start, then types a username/password or waits 60 seconds for you
   to log in. With a session saved by ``deal-hunter login``, the browser starts
   with its cookies, and the login form and wait are skipped while it's still
   logged in. The session is saved again after each login and search, so
   Facebook's refreshed cookies are kept. If Facebook logs the monitor out
   mid-run, it warns, or with facebook.require_session stops with an error
   before the logged-out search's listings are used.
4. The browser is sandboxed and gets no secrets. Playwright starts Chromium
   with ``--no-sandbox`` and the full environment, so a renderer bug on a
   Facebook page could read the API keys and the saved session. The browser
   now starts with Chromium's sandbox (facebook.browser_sandbox) and only
   locale/display/path variables (no HTTP(S)_PROXY or custom CA settings
   either). deal-hunter also sets the process umask to 077 and tightens
   ~/.ai-marketplace-monitor, so everything aimm writes (its log, cache and
   debug_*.html page dumps, which show a logged-in page) is owner-only.

5. Unattended, aimm's home config is ignored. aimm merges
   ``~/.ai-marketplace-monitor/config.toml`` into every run; with
   facebook.require_session nothing legitimate writes it, and a browser
   exploit could plant one (an [ai] base_url there would receive the API
   key). Deleting it at start can be raced, so aimm never reads it.

6. Searches report what they found. aimm 0.10.2 logs "Failed to get search
   results for <phrase> from <city>" as an error after *every* phrase search,
   whether or not it found anything. events.py drops that message; here each
   phrase search's real listing count is recorded as a ``search_results`` event
   (watch, phrase index, count; never the phrase or the location), with a
   warning when it is 0.

7. Rating summaries are plain text. aimm stores the rest of the model's
   ``Rating <n>:`` line as the summary, so a model that bolds its conclusion
   leaves a stray ``**`` in it. Both backends call ``AIResponse.to_cache`` on
   the response before returning it; here that call first cleans the comment
   (summary.py), so the cache, the notifiers and the dashboard all see the
   same text. If cleaning fails or leaves nothing, aimm's text is kept.

8. The score comes from the start of the rating line. aimm finds it with
   ``re.match(r".*Rating[^1-5]*([1-5])[:\\s]*(.*)", line)``, whose greedy ``.*``
   makes the *last* "Rating <n>" on the line win, so seller text copied into the
   summary ("... a fake 'Rating 5' note") could forge a 5. aimm's ``re`` is
   replaced by a proxy that answers that one pattern with an anchored match
   (``Rating <n>:`` at the start of the line, after any markdown) and passes
   every other call to the real ``re``. A line that doesn't start that way is
   not a rating line: aimm treats it as a continuation of the summary, or
   ignores it before the rating line, and a response with no rating line at all
   scores 1, with one warning per run. If a newer aimm changes that pattern
   literal, tests/test_launch.py fails (``test_rating_regex_patch_points_still_exist``)
   rather than letting the greedy match back in.

9. Discord is a notification method. aimm has no Discord notifier, and its
   notifiers are found through ``NotificationConfig.__subclasses__()``, so
   ``notify_all`` is wrapped instead. It is a classmethod that calls each
   subclass's ``notify`` and then recurses with ``subclass.notify_all(...)``;
   subclasses inherit the patched classmethod, so the card is posted only when
   ``cls is NotificationConfig`` (the call ``User.notify`` makes), never once
   per subclass. The result is ``upstream or discord_ok``, which is what makes
   aimm record the listings as notified (see discord.py).

10. Notification attempts are recorded. aimm logs a notifier's success at
    INFO and its failure at ERROR only after its own retries, and logs nothing
    structured. Every push notifier sends through
    ``NotificationConfig.send_message_with_retry`` (which answers a bool and
    doesn't raise) and email through ``EmailNotificationConfig.send_email_message``;
    both are wrapped here to record one ``notification`` event (method, outcome
    ``sent``/``failed``; never the title, topic or message), and Discord's
    post is recorded in ``notify_all_with_discord``. ``notify()`` has already
    decided there is something to send by then, so a batch of already-notified
    listings or an unconfigured notifier is not an attempt.

11. Traces follow the same patch points. With an OTLP endpoint set (tracing.py),
    ``MarketplaceMonitor.search_item`` (one watch's round) is the root
    ``deal_hunter.cycle`` span, and under it: ``get_listings`` (the phrase
    search: ``search_with_session``'s counted call), page loads
    (``goto_url_with_timeout``, one span for the load and its retries),
    ``FacebookMarketplace.get_listing_details``, ``OpenAIBackend.evaluate`` and
    ``AnthropicBackend.evaluate`` (every other backend inherits one), each
    notification attempt of 10, and the session save. Token counts, AI retries
    and the new-listing count come from aimm's own log events through
    ``TraceTap``, which reads them the way ``events.py`` does. Spans carry
    allowlisted numbers, names and outcomes only (tracing.py); never a phrase,
    listing text, URL, location or exception message. With tracing off every
    wrapper is a straight call.

``deal-hunter run`` starts aimm through ``main`` in the same process, which
applies all eleven. aimm is pinned, and tests/test_launch.py checks the patch points
still exist.

This deliberately has no ``python -m`` entry: ``-m`` puts the current
directory first on sys.path, so a planted package there would run with your
API keys in the environment.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Iterator
from logging import Logger
from typing import Any

from ai_marketplace_monitor.ai import AIResponse, AnthropicBackend, OpenAIBackend
from ai_marketplace_monitor.email_notify import EmailNotificationConfig
from ai_marketplace_monitor.facebook import FacebookMarketplace, FacebookSearchResultPage
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.notification import NotificationConfig
from ai_marketplace_monitor.utils import aimm_event, amm_home, is_substring
from playwright.sync_api import Error as PlaywrightError

from deal_hunter import discord, events, tracing
from deal_hunter import session as fb_session
from deal_hunter.discord import DiscordWebhook
from deal_hunter.session import Session, SessionError
from deal_hunter.settings import DEAL_HUNTER_NOTIFIERS, NOTIFIERS
from deal_hunter.summary import cap, clean_summary

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

# Only links with a scheme or www.: bare "domain/path" patterns also match spec
# text like "NUC8i5.BEH/16GB" or "3.5/4GHz" and would hide real listings.
# A link ends at the first character that can't appear in a URL, so invisible
# characters (zero-width space, soft hyphen, ...) can't hide the words after it.
_URL = re.compile(r"(?:https?://|www\.)[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", re.IGNORECASE)

# Facebook pages normally load in a few seconds; this only has to catch a load
# that will never finish.
NAV_TIMEOUT_MS = 60_000
# Same retry policy as aimm's goto_url: 10 retries, 5 s apart.
NAV_RETRIES = 10
NAV_RETRY_DELAY_S = 5


def strip_urls(text: str) -> str:
    return _URL.sub(" ", text)


def is_substring_without_urls(
    var1: str | list[str], var2: str | list[str], logger: Logger | None = None
) -> bool:
    """aimm's is_substring, with URLs removed from the text being searched."""
    text = strip_urls(var2) if isinstance(var2, str) else [strip_urls(s) for s in var2]
    return is_substring(var1, text, logger)


def goto_url_with_timeout(self: Any, url: str, attempt: int = 0) -> None:
    """aimm's Marketplace.goto_url, with a page-load timeout instead of none.

    As a trace, a page load is one ``deal_hunter.navigate`` span from the first
    try to the last: the retries (attempt > 0, called from inside the first)
    only raise the span's attempt count.
    """
    if attempt:
        tracing.annotate(tracing.NAVIGATE, tracing.ATTEMPT, attempt)
        _navigate(self, url, attempt)
        return
    with tracing.span(tracing.NAVIGATE, {tracing.ATTEMPT: 0, **_phrase_of(url)}) as span:
        _navigate(self, url, 0)
        span.outcome("ok")


def _phrase_of(url: str) -> dict[str, Any]:
    """The watch and phrase a page load belongs to, while a search is running.

    A search-page load is for the phrase about to be parsed; a listing page is
    for the one just parsed. The URL is only looked at, never recorded.
    """
    if _Search.watch is None:
        return {}
    attributes: dict[str, Any] = {tracing.WATCH: _Search.watch}
    n = max(_Search.phrases, 1)
    if "/marketplace/item/" in url and _Search.phrases_done:
        attributes[tracing.PHRASE_INDEX] = (_Search.phrases_done - 1) % n
    elif "/search?" in url:
        attributes[tracing.PHRASE_INDEX] = _Search.phrases_done % n
    return attributes


def _navigate(self: Any, url: str, attempt: int) -> None:
    try:
        if self.page is None:  # aimm asserts here; same retry path
            raise RuntimeError("no browser page")
        if self.logger:
            self.logger.debug(f"[Retrieve] Navigating to {url}")
        response = self.page.goto(url, timeout=NAV_TIMEOUT_MS)
        status = getattr(response, "status", None)
        if isinstance(status, int):
            tracing.annotate(tracing.NAVIGATE, tracing.STATUS_CODE, status)
        self.page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        if attempt == NAV_RETRIES:
            raise RuntimeError(
                f"Failed to navigate to {url} after {NAV_RETRIES} attempts. {e}"
            ) from e
        time.sleep(NAV_RETRY_DELAY_S)
        self.goto_url(url, attempt + 1)


# Set by main. SESSION None = aimm's own login only.
SESSION: Session | None = None
SANDBOX = True
_upstream_monitor_init = MarketplaceMonitor.__init__
_upstream_login = FacebookMarketplace.login
_upstream_search = FacebookMarketplace.search
_upstream_get_listings = FacebookSearchResultPage.get_listings
_upstream_to_cache = AIResponse.to_cache
_summary_warned = False
# The plain function behind aimm's classmethod: calling it with a subclass as cls
# keeps aimm's own recursion (`cls.__subclasses__()`) walking that subclass.
_upstream_notify_all = NotificationConfig.__dict__["notify_all"].__func__
_upstream_send_with_retry = NotificationConfig.send_message_with_retry
_upstream_send_email = EmailNotificationConfig.send_email_message
_upstream_search_item = MarketplaceMonitor.search_item
_upstream_get_listing_details = FacebookMarketplace.get_listing_details
_upstream_openai_evaluate = OpenAIBackend.evaluate
_upstream_anthropic_evaluate = AnthropicBackend.evaluate
# The values a notification event's method may take (metrics label values).
NOTIFY_METHODS = (*NOTIFIERS, *DEAL_HUNTER_NOTIFIERS)
NOTIFY_LOGGER = logging.getLogger("monitor")
# Set by main when the discord method is enabled. None = aimm's notifiers only.
DISCORD: DiscordWebhook | None = None

# The literal in both backends' evaluate (checked by tests/test_launch.py), and
# the strict form: a line-initial "rating" (after whitespace and markdown only: not
# quotes, ">" or bullets, which a listing could use to forge a line), the FIRST
# number after it (1-5, optionally "/5", never part of a longer number), then an
# optional closer and separator before the summary. No second "Rating" is reachable.
AIMM_RATING_PATTERN = r".*Rating[^1-5]*([1-5])[:\s]*(.*)"
_STRICT_RATING = re.compile(
    r"^[\s*_#]*rating(?![A-Za-z])[\s:\-\u2013\u2014]*([1-5])(?:\s*/\s*5)?(?![A-Za-z0-9/]|\.\d)"
    r"[\s*_]*(?:[:\-\u2013\u2014][\s*_]*)?(.*)$",
    re.IGNORECASE,
)
_rating_warned = False


class StrictRatingRe:
    """Stands in for ``re`` inside ai_marketplace_monitor.ai (see the module docstring, 8)."""

    def match(self, pattern: str, string: str, flags: int = 0) -> re.Match[str] | None:
        global _rating_warned
        if pattern != AIMM_RATING_PATTERN:
            return re.match(pattern, string, flags)
        matched = _STRICT_RATING.match(string)
        if matched is None and not _rating_warned and re.match(pattern, string):
            _rating_warned = True  # event only: never the AI's or the listing's text
            logging.getLogger("monitor").warning(
                "[AI] A rating line didn't match the expected format; scored as unrated. "
                "Further cases are not logged."
            )
        return matched

    def __getattr__(self, name: str) -> Any:
        return getattr(re, name)


def to_cache_with_clean_summary(
    self: Any,
    listing: Any,
    item_config: Any,
    marketplace_config: Any,
    local_cache: Any = None,
) -> None:
    """aimm's AIResponse.to_cache, after cleaning the summary it is about to store.

    The response object is the one aimm returns to its caller, so the notifiers
    get the cleaned text too. A rating is never dropped over its summary: if
    cleaning fails or leaves nothing, aimm's text is stored (and one warning
    is logged per run).
    """
    global _summary_warned
    try:
        cleaned = clean_summary(self.comment)
    except Exception as e:  # noqa: BLE001 — never lose a rating over its summary text
        cleaned, reason = "", f"{type(e).__name__}"
    else:
        reason = "it was empty after cleaning"
    if cleaned:
        self.comment = cleaned
    elif isinstance(self.comment, str):
        self.comment = cap(self.comment)  # the length cap holds on every path
    if not cleaned and not _summary_warned:
        _summary_warned = True
        logging.getLogger("monitor").warning(
            f"[AI] Kept an AI rating summary as written: {reason}. Further cases are not logged."
        )
    _upstream_to_cache(self, listing, item_config, marketplace_config, local_cache)


def record_notification(logger: Logger | None, method: Any, ok: bool) -> None:
    """Log one notification attempt as a structured event (see the module docstring, 10)."""
    label = method if method in NOTIFY_METHODS else "other"
    outcome = "sent" if ok else "failed"
    event = aimm_event("notification", method=label, outcome=outcome)
    (logger or NOTIFY_LOGGER).info(f"[Notify] {label}: {outcome}", extra=event)


def _recorded(method: Any, logger: Logger | None, send: Callable[[], Any]) -> bool:
    label = method if method in NOTIFY_METHODS else "other"
    with tracing.span(tracing.NOTIFY, {tracing.NOTIFY_METHOD: label}) as span:
        try:
            ok = bool(send())
        except Exception:
            record_notification(logger, method, False)
            raise
        record_notification(logger, method, ok)
        span.outcome("sent" if ok else "failed")
        if not ok:
            span.fail()
    return ok


def send_message_recorded(
    self: Any, title: str, message: str, logger: Logger | None = None
) -> bool:
    """NotificationConfig.send_message_with_retry, recording whether the message went."""
    return _recorded(
        self.notify_method, logger, lambda: _upstream_send_with_retry(self, title, message, logger)
    )


def send_email_recorded(self: Any, *args: Any, **kwargs: Any) -> bool:
    """EmailNotificationConfig.send_email_message, recording whether the email went."""
    return _recorded(
        self.notify_method,
        kwargs.get("logger"),
        lambda: _upstream_send_email(self, *args, **kwargs),
    )


def notify_all_with_discord(
    cls: Any, /, config: Any, listings: Any, ratings: Any, statuses: Any, **kwargs: Any
) -> bool:
    """aimm's NotificationConfig.notify_all, then the Discord cards on the top-level call.

    aimm calls this once per batch (User.notify) and the upstream body calls
    ``subclass.notify_all`` for every notifier class, which reaches this wrapper
    again with cls set to that subclass: only the call with cls is
    NotificationConfig posts. The return value is what aimm uses to decide
    whether to mark the listings notified, so Discord's success counts exactly
    like another notifier's. Discord problems are logged, never raised.
    """
    upstream_result: bool = _upstream_notify_all(cls, config, listings, ratings, statuses, **kwargs)
    if cls is not NotificationConfig or DISCORD is None:
        return upstream_result
    force = bool(kwargs.get("force", False))
    # Only a batch with something new is an attempt (discord.notify sends nothing,
    # and answers False, when every listing was already notified).
    attempt = force or any(getattr(s, "name", "") != "NOTIFIED" for s in statuses)
    with tracing.span(tracing.NOTIFY, {tracing.NOTIFY_METHOD: "discord"}, record=attempt) as span:
        try:
            discord_ok = discord.notify(DISCORD, listings, ratings, statuses, force=force)
        except Exception as e:  # noqa: BLE001 — a card bug must not stop the other notifiers' result
            NOTIFY_LOGGER.error(f"[Discord] Couldn't build the cards: {type(e).__name__}")
            discord_ok = False
            span.fail(e)
        if attempt:
            record_notification(NOTIFY_LOGGER, "discord", discord_ok)
            span.outcome("sent" if discord_ok else "failed")
            if not discord_ok:
                span.fail()
    return upstream_result or discord_ok


def create_page_with_session(self: Any, swap_proxy: bool = False) -> Any:
    """aimm's Marketplace.create_page, with the saved session loaded into new contexts."""
    if self.browser is None:
        raise RuntimeError("no browser")
    if (
        self.page
        and swap_proxy
        and self.config.monitor_config is not None
        and isinstance(self.config.monitor_config.proxy_server, list)
        and len(self.config.monitor_config.proxy_server) > 1
    ):
        self.page.close()
        self.page = None

    if self.page is None:
        options: dict[str, Any] = {
            "proxy": (
                None
                if self.config.monitor_config is None
                else self.config.monitor_config.get_proxy_options()
            )
        }
        state = fb_session.load(SESSION.path) if SESSION else None
        if state is not None:  # filtered here too, in case an older save wasn't
            options["storage_state"] = fb_session.facebook_only(state)
        self.page = self.browser.new_context(**options).new_page()
    return self.page


def monitor_init(self: Any, config_files: Any, headless: Any, logger: Any) -> None:
    """MarketplaceMonitor.__init__, without aimm's home config.toml when unattended."""
    _upstream_monitor_init(self, config_files, headless, logger)
    if SESSION is not None and SESSION.required:
        home_config = amm_home / "config.toml"  # the path upstream adds
        resolved = home_config.resolve()
        self.config_files = [
            f for f in self.config_files if f != home_config and f.resolve() != resolved
        ]


def launch_browser(self: Any) -> Any:
    """MarketplaceMonitor._launch_browser: Chromium only, sandboxed, without secrets."""
    try:
        browser = self.playwright.chromium.launch(
            headless=self.headless, chromium_sandbox=SANDBOX, env=fb_session.browser_env()
        )
    except PlaywrightError as e:
        hint = (
            " If the sandbox can't start here (some containers), set "
            "facebook.browser_sandbox = false."
            if SANDBOX
            else ""
        )
        raise RuntimeError(
            f"couldn't start Chromium: {fb_session.first_line(e)}. "
            f"Run `uv run playwright install chromium`.{hint}"
        ) from None
    if self.logger:
        # The dashboard counts browser_ready as the monitor (re)starting.
        event = aimm_event("browser_ready", engine="chromium", sandbox=SANDBOX)
        if SANDBOX:
            self.logger.info("[Browser] Started Chromium (sandboxed).", extra=event)
        else:
            self.logger.warning(
                "[Browser] Started Chromium WITHOUT its sandbox (facebook.browser_sandbox).",
                extra=event,
            )
    return browser


def login_with_session(self: Any) -> None:
    """FacebookMarketplace.login that reuses the saved session while it's valid."""
    session = SESSION
    self.hunter_logged_in = False
    if session is not None and fb_session.load(session.path) is not None:
        self.page = self.create_page(swap_proxy=True)
        self.goto_url(fb_session.MARKETPLACE_URL)
        if fb_session.logged_in(self.page):
            self.hunter_logged_in = True
            _save(self, session)
            if self.logger:
                self.logger.info("[Login] Using the saved Facebook session.")
            return
        if self.logger:
            self.logger.warning(
                "[Login] The saved Facebook session has expired. "
                "Run `deal-hunter login` to save a new one."
            )
    if session is not None and session.required:
        raise SessionError(
            f"no valid Facebook session in {session.path}, and facebook.require_session "
            "is set. Run `deal-hunter login` to save one."
        )
    _upstream_login(self)
    if session is not None and self.page is not None and fb_session.logged_in(self.page):
        self.hunter_logged_in = True
        _save(self, session)


class _Search:
    """The watch being searched and how many phrases of it have come back.

    aimm searches one watch at a time, so one module-level record is enough.
    """

    watch: str | None = None
    phrases = 0  # in the watch
    phrases_done = 0


def get_listings_counted(self: Any) -> Any:
    """FacebookSearchResultPage.get_listings, recording how many listings it found.

    aimm calls it once per search phrase, for each city in turn, so within a
    watch's search the phrase index is the number of calls so far, modulo the
    watch's phrase count. That call is the ``deal_hunter.search`` span: the
    page load before it is its own ``deal_hunter.navigate`` span.
    """
    watch = _Search.watch
    if watch is None:
        return _upstream_get_listings(self)
    index = _Search.phrases_done % max(_Search.phrases, 1)
    attributes = {tracing.WATCH: watch, tracing.PHRASE_INDEX: index}
    with tracing.span(tracing.SEARCH, attributes) as span:
        listings = _upstream_get_listings(self)
        span.set(tracing.count("listings"), len(listings))
        span.outcome("ok" if listings else "empty")
    _Search.phrases_done += 1
    logger = self.logger
    if logger:
        count = len(listings)
        event = aimm_event("search_results", item=watch, phrase_index=index, count=count)
        logger.info(f"[Search] {count} listings for phrase #{index}", extra=event)
        if count == 0:
            logger.warning(f"No search results for watch '{watch}' phrase #{index}")
    return listings


def search_item_traced(
    self: Any, marketplace_config: Any, marketplace: Any, item_config: Any
) -> None:
    """MarketplaceMonitor.search_item, as the root ``deal_hunter.cycle`` span.

    aimm schedules each watch as its own job, so a cycle is one watch's round:
    its phrase searches, listing fetches, AI ratings and notifications.
    """
    if not tracing.enabled():
        return _upstream_search_item(self, marketplace_config, marketplace, item_config)
    with tracing.span(tracing.CYCLE, {tracing.WATCH: getattr(item_config, "name", None)}) as span:
        _upstream_search_item(self, marketplace_config, marketplace, item_config)
        span.outcome("ok")
    return None


def get_listing_details_traced(self: Any, *args: Any, **kwargs: Any) -> Any:
    """FacebookMarketplace.get_listing_details, as a ``deal_hunter.listing`` span.

    aimm answers ``(listing, from_cache)``; a listing already in its cache costs
    no page load, and shows as outcome "cached".
    """
    with tracing.span(tracing.LISTING) as span:
        result = _upstream_get_listing_details(self, *args, **kwargs)
        cached = isinstance(result, tuple) and len(result) == 2 and result[1] is True
        span.outcome("cached" if cached else "ok")
    return result


def _traced_evaluate(upstream: Callable[..., Any]) -> Callable[..., Any]:
    """An AI backend's ``evaluate`` as a ``deal_hunter.ai_rate`` span."""

    def evaluate(self: Any, listing: Any, item_config: Any, marketplace_config: Any) -> Any:
        if not tracing.enabled():
            return upstream(self, listing, item_config, marketplace_config)
        config = self.config
        provider = getattr(config, "provider", None) or getattr(config, "name", None)
        attributes = {
            tracing.GEN_AI_SYSTEM: provider.lower() if isinstance(provider, str) else None,
            tracing.GEN_AI_MODEL: getattr(config, "model", None)
            or getattr(self, "default_model", None),
        }
        with tracing.span(tracing.AI_RATE, attributes) as span:
            rating = upstream(self, listing, item_config, marketplace_config)
            span.set(tracing.RATING, getattr(rating, "score", None))
            span.outcome("ok")
        return rating

    evaluate.__name__ = "evaluate"
    return evaluate


openai_evaluate_traced = _traced_evaluate(_upstream_openai_evaluate)
anthropic_evaluate_traced = _traced_evaluate(_upstream_anthropic_evaluate)


# aimm's hilight() markup around the tags it logs the API response and errors under.
_AI_RESPONSE = "[blue][AI-Response][/blue]"
_AI_ERROR = "[red][AI-Error][/red]"


class TraceTap(logging.Handler):
    """Hands aimm's own log events to the span they belong to.

    Token counts are in the debug dump of the API response (``events.ai_usage``
    reads them for the events file, from the response's usage block only), an
    AI retry is an ``[AI-Error]`` line, and ``search_summary`` carries how many
    new listings the round found. Only integers are taken; no text is kept.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not tracing.enabled():
                return
            aimm = getattr(record, "aimm", None)
            if isinstance(aimm, dict):
                if aimm.get("kind") == "search_summary":
                    tracing.annotate(tracing.CYCLE, tracing.count("new"), aimm.get("new_count"))
                return
            # Only aimm's own lines from the backends' evaluate: a listing's title
            # can sit anywhere else in a message, but never first in one of these.
            if record.funcName != "evaluate":
                return
            message = record.getMessage()
            if message.startswith(_AI_RESPONSE):
                usage = events.ai_usage(message, 0.0)
                if usage:
                    tracing.annotate(tracing.AI_RATE, tracing.TOKENS_IN, usage.get("input_tokens"))
                    tracing.annotate(
                        tracing.AI_RATE, tracing.TOKENS_OUT, usage.get("output_tokens")
                    )
            elif message.startswith(_AI_ERROR):
                tracing.increment(tracing.AI_RATE, tracing.ATTEMPT)
        except Exception:  # noqa: BLE001, S110 — never break the monitor over a trace
            pass


def search_with_session(self: Any, item_config: Any) -> Iterator[Any]:
    """FacebookMarketplace.search, checking the login survived it.

    Checked when the first listing arrives (the search page is loaded) and
    again at the end, so a required session stops the monitor before
    logged-out results go to the AI and notifiers. While it runs,
    get_listings_counted knows which watch is being searched.
    """
    checked = False
    _Search.watch, _Search.phrases_done = getattr(item_config, "name", None), 0
    _Search.phrases = len(getattr(item_config, "search_phrases", None) or [])
    try:
        for listing in _upstream_search(self, item_config):
            if not checked:
                checked = True
                _check_login(self)
            yield listing
    finally:
        _Search.watch = None
    _check_login(self)


def _check_login(market: Any) -> None:
    session = SESSION
    if session is None or market.page is None or not getattr(market, "hunter_logged_in", False):
        return
    if fb_session.logged_in(market.page):
        _save(market, session)
        return
    market.hunter_logged_in = False
    if session.required:
        raise SessionError(
            "Facebook logged the monitor out. Run `deal-hunter login` to save a new session."
        )
    if market.logger:
        market.logger.warning(
            "[Login] Facebook logged the monitor out; searching without a login until "
            "the next start. Run `deal-hunter login` to save a new session."
        )


def _save(market: Any, session: Session) -> None:
    try:
        with tracing.span(tracing.SESSION_SAVE) as span:
            excluded = fb_session.save(market.page.context, session.path)
            span.outcome("ok")
    except (OSError, SessionError, PlaywrightError) as e:  # keep running
        reason = e.strerror if isinstance(e, OSError) else fb_session.first_line(e)
        if market.logger:
            market.logger.warning(f"[Login] Couldn't save the Facebook session: {reason}")
        return
    if not excluded and not getattr(market, "hunter_backup_warned", False):
        market.hunter_backup_warned = True
        if market.logger:
            market.logger.warning(
                f"[Login] Couldn't exclude {session.path} from Time Machine backups."
            )


_CONFIG_READ_BYTES = 1_000_000


def _config_paths(args: list[str]) -> list[str]:
    """The files named by --config / -r in aimm's arguments."""
    paths = []
    for i, arg in enumerate(args):
        if arg in ("--config", "-r") and i + 1 < len(args):
            paths.append(args[i + 1])
        elif arg.startswith("--config="):
            paths.append(arg.partition("=")[2])
    return paths


def hidden_values(args: list[str]) -> list[str]:
    """The Marketplace location(s) in the rendered config named by args.

    Recorded messages replace them with "[location]". The config is ours
    (rendered by `run`), but it sits in aimm's home, so it is read like any
    file there: not through a link, size-capped, values type-checked. If the
    location can't be found, say so once: nothing is hidden beyond the usual
    secret rules.
    """
    for path in _config_paths(args):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                data = tomllib.loads(f.read(_CONFIG_READ_BYTES).decode("utf-8"))
        except (OSError, ValueError, RecursionError):  # unreadable, a link, or not TOML
            continue
        section: Any = data
        for key in ("marketplace", "facebook"):
            section = section.get(key) if isinstance(section, dict) else None
        city = section.get("search_city") if isinstance(section, dict) else None
        values = [city] if isinstance(city, str) else city if isinstance(city, list) else []
        found = [v for v in values if isinstance(v, str) and v.strip()]
        if found:
            return found
    print(
        "deal-hunter: couldn't read the Marketplace location from the config, so recorded "
        "messages can't hide it.",
        file=sys.stderr,
    )
    return []


def main(
    args: list[str],
    session: Session | None = None,
    sandbox: bool = True,
    discord_url: str | None = None,
) -> None:
    """Patch aimm, then run aimm's CLI with args. Exits via SystemExit."""
    global SESSION, SANDBOX, DISCORD
    from ai_marketplace_monitor import ai, facebook, marketplace, monitor
    from ai_marketplace_monitor.cli import app

    SESSION, SANDBOX = session, sandbox
    DISCORD = DiscordWebhook(discord_url) if discord_url else None
    os.umask(0o077)
    fb_session.private_home()  # files aimm already created, before the umask
    # For `deal-hunter web`: aimm logs to the "monitor" logger.
    events.attach(
        logging.getLogger("monitor"),
        events.events_path(fb_session.AIMM_HOME, migrate=True),
        hidden=hidden_values(args),
    )
    setattr(facebook, "is_substring", is_substring_without_urls)  # noqa: B010 — not re-exported
    marketplace.Marketplace.goto_url = goto_url_with_timeout  # type: ignore[method-assign]
    marketplace.Marketplace.create_page = create_page_with_session  # type: ignore[method-assign]
    facebook.FacebookMarketplace.login = login_with_session  # type: ignore[method-assign]
    facebook.FacebookMarketplace.search = search_with_session  # type: ignore[method-assign,assignment]
    facebook.FacebookSearchResultPage.get_listings = get_listings_counted  # type: ignore[method-assign]
    setattr(ai, "re", StrictRatingRe())  # noqa: B010 — aimm's own module-level import
    ai.AIResponse.to_cache = to_cache_with_clean_summary  # type: ignore[method-assign]
    NotificationConfig.notify_all = classmethod(notify_all_with_discord)  # type: ignore[assignment]
    NotificationConfig.send_message_with_retry = send_message_recorded  # type: ignore[method-assign]
    EmailNotificationConfig.send_email_message = send_email_recorded  # type: ignore[method-assign]
    monitor.MarketplaceMonitor.search_item = search_item_traced  # type: ignore[method-assign]
    facebook.FacebookMarketplace.get_listing_details = get_listing_details_traced  # type: ignore[method-assign]
    ai.OpenAIBackend.evaluate = openai_evaluate_traced  # type: ignore[method-assign]
    ai.AnthropicBackend.evaluate = anthropic_evaluate_traced  # type: ignore[method-assign]
    monitor_logger = logging.getLogger("monitor")
    if tracing.enabled() and not any(isinstance(h, TraceTap) for h in monitor_logger.handlers):
        monitor_logger.addHandler(TraceTap())
    monitor.MarketplaceMonitor._launch_browser = launch_browser  # type: ignore[method-assign]
    monitor.MarketplaceMonitor.__init__ = monitor_init  # type: ignore[method-assign]
    app(args=args, prog_name="ai-marketplace-monitor")
