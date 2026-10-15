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

``deal-hunter run`` starts aimm through ``main`` in the same process, which
applies all five. aimm is pinned, and tests/test_launch.py checks the patch points
still exist.

This deliberately has no ``python -m`` entry: ``-m`` puts the current
directory first on sys.path, so a planted package there would run with your
API keys in the environment.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterator
from logging import Logger
from typing import Any

from ai_marketplace_monitor.facebook import FacebookMarketplace
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.utils import aimm_event, amm_home, is_substring
from playwright.sync_api import Error as PlaywrightError

from deal_hunter import events
from deal_hunter import session as fb_session
from deal_hunter.session import Session, SessionError

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
    """aimm's Marketplace.goto_url, with a page-load timeout instead of none."""
    try:
        if self.page is None:  # aimm asserts here; same retry path
            raise RuntimeError("no browser page")
        if self.logger:
            self.logger.debug(f"[Retrieve] Navigating to {url}")
        self.page.goto(url, timeout=NAV_TIMEOUT_MS)
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
        self.config_files = [f for f in self.config_files if f != home_config]


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


def search_with_session(self: Any, item_config: Any) -> Iterator[Any]:
    """FacebookMarketplace.search, checking the login survived it.

    Checked when the first listing arrives (the search page is loaded) and
    again at the end, so a required session stops the monitor before
    logged-out results go to the AI and notifiers.
    """
    checked = False
    for listing in _upstream_search(self, item_config):
        if not checked:
            checked = True
            _check_login(self)
        yield listing
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
        excluded = fb_session.save(market.page.context, session.path)
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


def main(args: list[str], session: Session | None = None, sandbox: bool = True) -> None:
    """Patch aimm, then run aimm's CLI with args. Exits via SystemExit."""
    global SESSION, SANDBOX
    from ai_marketplace_monitor import facebook, marketplace, monitor
    from ai_marketplace_monitor.cli import app

    SESSION, SANDBOX = session, sandbox
    os.umask(0o077)
    fb_session.private_home()  # files aimm already created, before the umask
    # For `deal-hunter web`: aimm logs to the "monitor" logger.
    events.attach(
        logging.getLogger("monitor"), events.events_path(fb_session.AIMM_HOME, migrate=True)
    )
    setattr(facebook, "is_substring", is_substring_without_urls)  # noqa: B010 — not re-exported
    marketplace.Marketplace.goto_url = goto_url_with_timeout  # type: ignore[method-assign]
    marketplace.Marketplace.create_page = create_page_with_session  # type: ignore[method-assign]
    facebook.FacebookMarketplace.login = login_with_session  # type: ignore[method-assign]
    facebook.FacebookMarketplace.search = search_with_session  # type: ignore[method-assign,assignment]
    monitor.MarketplaceMonitor._launch_browser = launch_browser  # type: ignore[method-assign]
    monitor.MarketplaceMonitor.__init__ = monitor_init  # type: ignore[method-assign]
    app(args=args, prog_name="ai-marketplace-monitor")
