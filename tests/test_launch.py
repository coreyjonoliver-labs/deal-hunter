"""The URL-blind matcher, and that it still patches the matcher aimm's filters use."""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ai_marketplace_monitor import facebook, marketplace, monitor, utils
from fakes import LOGIN_COOKIE, FakeBrowser, FakeContext

from nuc_hunter import events, launch, profile
from nuc_hunter import session as fb_session
from nuc_hunter.session import Session, SessionError

TRIMMER = (
    "Toro 60V Revolution String Trimmer (battery not included)  "
    "New Toro string trimmer. More info: "
    "https://www.toro.com/en/product/66110T?srsltid=AU7gw4W8SzH4PNq-RLqNuc"
)


def test_url_does_not_pass_keyword_gate() -> None:
    assert utils.is_substring(profile.NUC_KEYWORDS, TRIMMER)  # the bug, upstream
    assert not launch.is_substring_without_urls(profile.NUC_KEYWORDS, TRIMMER)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/nuc",
        "https://www.facebook.com/marketplace/item/1/?x=Nuc",
        "www.example.com/beelink",
        "HTTPS://EXAMPLE.COM/NUC",
    ],
)
def test_strip_urls(url: str) -> None:
    assert not launch.is_substring_without_urls(profile.NUC_KEYWORDS, f"Lawn mower, see {url} ok")


@pytest.mark.parametrize(
    "text",
    [
        "Intel NUC11 i5, specs at https://ark.intel.com/content/www/us/en/ark.html",
        "Beelink SER5 (beelink.com/ser5) 16GB",
        "Minisforum UM790 Pro 32GB/1TB",  # a slash in specs is not a URL
        "GMKtec NucBox G3 N100 8GB/256GB",
        # Dotted specs with slashes are not links.
        "Intel NUC8i5.BEH/16GB",
        "NUC.8i5/256GB",
        "Small form factor, 3.5/4GHz nuc",
        "$100.00/obo nuc",
        "i5.8th/16GB nuc",
        "Bare domain beelink.com/ser5 only",
        "See https://x.com/a), nuc here",
    ],
)
def test_real_listings_with_urls_still_pass(text: str) -> None:
    assert launch.is_substring_without_urls(profile.NUC_KEYWORDS, text)


def test_antikeyword_inside_url_does_not_drop_listing() -> None:
    text = "Beelink EQ12 N100 16GB, photos: https://imgur.com/a/xWTBq9"
    assert utils.is_substring(profile.ANTIKEYWORDS, text)  # "wtb" in the link
    assert not launch.is_substring_without_urls(profile.ANTIKEYWORDS, text)


@pytest.mark.parametrize(
    "sep", ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u180e", "\u2061"]
)
def test_zero_width_char_ends_a_link(sep: str) -> None:
    text = f"Intel NUC http://x{sep}for parts"
    assert launch.is_substring_without_urls(profile.ANTIKEYWORDS, text)


def test_list_of_texts() -> None:
    assert not launch.is_substring_without_urls(["nuc"], ["a https://x.io/nuc", "b"])
    assert launch.is_substring_without_urls(["nuc"], ["a https://x.io/", "NUC"])


def test_patch_point_still_exists() -> None:
    # If aimm stops filtering through facebook.is_substring, the patch would
    # silently do nothing.
    assert facebook.is_substring is utils.is_substring
    source = inspect.getsource(facebook.FacebookMarketplace.check_listing)
    assert "is_substring(keywords" in source and "is_substring(antikeywords" in source


@pytest.fixture
def request_finalizer(request: pytest.FixtureRequest) -> list[Any]:
    finalizers: list[Any] = []
    request.addfinalizer(lambda: [f() for f in finalizers])
    return finalizers


def test_main_patches_then_runs_aimm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request_finalizer: list[Any]
) -> None:
    import ai_marketplace_monitor.cli

    seen = []
    calls = []
    monkeypatch.setattr(facebook, "is_substring", utils.is_substring)
    # Registered with monkeypatch so main()'s patches are undone afterwards.
    for cls, name in [
        (marketplace.Marketplace, "goto_url"),
        (marketplace.Marketplace, "create_page"),
        (facebook.FacebookMarketplace, "login"),
        (facebook.FacebookMarketplace, "search"),
        (monitor.MarketplaceMonitor, "_launch_browser"),
    ]:
        monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(launch, "SESSION", None)
    monkeypatch.setattr(launch, "SANDBOX", True)
    old_umask = os.umask(0o022)
    request_finalizer.append(lambda: os.umask(old_umask))

    def fake_app(**kwargs: object) -> None:
        seen.append(facebook.is_substring)
        calls.append(kwargs)

    monkeypatch.setattr(ai_marketplace_monitor.cli, "app", fake_app)
    sess = Session(tmp_path / "s.json", required=True)
    launch.main(["--config", "c.toml"], sess, sandbox=False)
    assert calls == [{"args": ["--config", "c.toml"], "prog_name": "ai-marketplace-monitor"}]
    assert seen == [launch.is_substring_without_urls]
    assert marketplace.Marketplace.goto_url is launch.goto_url_with_timeout
    assert marketplace.Marketplace.create_page is launch.create_page_with_session
    assert facebook.FacebookMarketplace.login is launch.login_with_session
    assert facebook.FacebookMarketplace.search is launch.search_with_session
    assert monitor.MarketplaceMonitor._launch_browser is launch.launch_browser
    assert launch.SESSION == sess and launch.SANDBOX is False
    assert os.umask(0o022) == 0o077  # files aimm writes are owner-only
    monitor_logger = logging.getLogger("monitor")
    recorders = [h for h in monitor_logger.handlers if isinstance(h, events.EventRecorder)]
    for h in recorders:
        monitor_logger.removeHandler(h)
    assert [h.path for h in recorders] == [fb_session.AIMM_HOME / events.EVENTS_NAME]
    # The patched gate drops the trimmer and keeps a real listing.
    assert not seen[0](profile.NUC_KEYWORDS, TRIMMER)
    assert seen[0](profile.NUC_KEYWORDS, "Intel NUC8i5.BEH/16GB, https://x.io/a")


def test_goto_patch_point_still_exists() -> None:
    # The patch replaces aimm's goto_url wholesale. If upstream changes it
    # (say, adds a timeout itself), re-check the copy in launch.py.
    source = inspect.getsource(marketplace.Marketplace.goto_url)
    assert "self.page.goto(url, timeout=0)" in source
    assert "attempt == 10" in source and "time.sleep(5)" in source
    assert "goto_url" not in vars(facebook.FacebookMarketplace)  # not overridden


class FakePage:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.goto_timeouts: list[float] = []
        self.load_timeouts: list[float] = []

    def goto(self, url: str, *, timeout: float) -> None:
        self.goto_timeouts.append(timeout)
        if self.failures:
            self.failures -= 1
            raise TimeoutError("Timeout 60000ms exceeded.")

    def wait_for_load_state(self, state: str, *, timeout: float) -> None:
        self.load_timeouts.append(timeout)


class FakeMarketplace:
    goto_url = launch.goto_url_with_timeout
    logger = None

    def __init__(self, page: FakePage) -> None:
        self.page = page


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(launch.time, "sleep", sleeps.append)
    return sleeps


def test_goto_uses_a_timeout(no_sleep: list[float]) -> None:
    page = FakePage(failures=0)
    FakeMarketplace(page).goto_url("https://www.facebook.com/marketplace/x")
    assert page.goto_timeouts == [launch.NAV_TIMEOUT_MS]
    assert page.load_timeouts == [launch.NAV_TIMEOUT_MS]
    assert all(t > 0 for t in page.goto_timeouts)  # 0 would mean wait forever
    assert no_sleep == []


def test_goto_retries_a_timed_out_load(no_sleep: list[float]) -> None:
    page = FakePage(failures=3)
    FakeMarketplace(page).goto_url("https://www.facebook.com/marketplace/x")
    assert len(page.goto_timeouts) == 4
    assert no_sleep == [launch.NAV_RETRY_DELAY_S] * 3


def test_goto_gives_up_with_an_error(no_sleep: list[float]) -> None:
    page = FakePage(failures=launch.NAV_RETRIES + 1)
    with pytest.raises(RuntimeError, match="after 10 attempts"):
        FakeMarketplace(page).goto_url("https://www.facebook.com/marketplace/x")
    assert len(page.goto_timeouts) == launch.NAV_RETRIES + 1


def test_goto_lets_ctrl_c_through(no_sleep: list[float]) -> None:
    class InterruptedPage(FakePage):
        def goto(self, url: str, *, timeout: float) -> None:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        FakeMarketplace(InterruptedPage(failures=0)).goto_url("https://x.example/")
    assert no_sleep == []


def test_goto_retries_a_stalled_load_state(no_sleep: list[float]) -> None:
    class StallingPage(FakePage):
        def wait_for_load_state(self, state: str, *, timeout: float) -> None:
            super().wait_for_load_state(state, timeout=timeout)
            if len(self.load_timeouts) == 1:
                raise TimeoutError("Timeout 60000ms exceeded.")

    page = StallingPage(failures=0)
    FakeMarketplace(page).goto_url("https://www.facebook.com/marketplace/x")
    assert len(page.goto_timeouts) == 2
    assert no_sleep == [launch.NAV_RETRY_DELAY_S]


def test_goto_without_a_page_retries_then_errors(no_sleep: list[float]) -> None:
    market = FakeMarketplace(FakePage(failures=0))
    market.page = None  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="no browser page"):
        market.goto_url("https://www.facebook.com/marketplace/x")
    assert len(no_sleep) == launch.NAV_RETRIES


# --- Saved Facebook session ---------------------------------------------------


# Upstream functions launch.py replaces with an edited copy. A new aimm release
# that changes either fails here: re-copy it into launch.py, then update the hash.
COPIED = {
    "create_page": "aabbed61f7d1025edf9b250b58159392f188c85e085cad7635404644ed1da31f",
    "_launch_browser": "f5d7ed291bbd210ff829736eee16e325a6c8acb7748ce03cda513945cc2734cf",
}


@pytest.mark.parametrize(
    ("func", "name"),
    [
        (marketplace.Marketplace.create_page, "create_page"),
        (monitor.MarketplaceMonitor._launch_browser, "_launch_browser"),
    ],
)
def test_copied_upstream_code_is_unchanged(func: Any, name: str) -> None:
    source = inspect.getsource(func)
    assert hashlib.sha256(source.encode()).hexdigest() == COPIED[name]


def test_session_patch_points_still_exist() -> None:
    # create_page is replaced wholesale, and login/search are wrapped. If
    # upstream changes them, re-check launch.py.
    create = inspect.getsource(marketplace.Marketplace.create_page)
    assert "context = self.browser.new_context(" in create
    assert "proxy=" in create and "get_proxy_options()" in create
    assert "storage_state" not in create
    assert "create_page" not in vars(facebook.FacebookMarketplace)
    login = inspect.getsource(facebook.FacebookMarketplace.login)
    assert "self.page = self.create_page(swap_proxy=True)" in login
    search = inspect.getsource(facebook.FacebookMarketplace.search)
    assert "if not self.page:\n            self.login()" in search
    assert inspect.isgeneratorfunction(facebook.FacebookMarketplace.search)
    assert launch._upstream_login.__qualname__ == "FacebookMarketplace.login"
    assert launch._upstream_search.__qualname__ == "FacebookMarketplace.search"


class SessionMarket:
    """Enough of FacebookMarketplace for the session patches."""

    create_page = launch.create_page_with_session
    login = launch.login_with_session
    search = launch.search_with_session

    def __init__(self, browser: FakeBrowser, logged_in_after_goto: bool | None = None) -> None:
        self.browser = browser
        self.page: Any = None
        self.logger = logging.getLogger("test-session")
        self.config = SimpleNamespace(monitor_config=None)
        self.logged_in_after_goto = logged_in_after_goto
        self.visited: list[str] = []

    def goto_url(self, url: str, attempt: int = 0) -> None:
        self.visited.append(url)
        self.page.url = url
        if self.logged_in_after_goto is False:  # Facebook drops the login
            self.page.context.cookie_list.clear()


@pytest.fixture
def saved(tmp_path: Path) -> Path:
    path = tmp_path / "s.json"
    fb_session.save(FakeContext([LOGIN_COOKIE]), path)
    return path


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replaces aimm's own login with one that 'logs in' and records the call."""
    calls: list[Any] = []

    def fake_login(self: Any) -> None:
        calls.append(self)
        self.page = self.create_page(swap_proxy=True)
        self.page.url = "https://www.facebook.com/"
        self.page.context.cookie_list[:] = [LOGIN_COOKIE]

    monkeypatch.setattr(launch, "_upstream_login", fake_login)
    return calls


def test_create_page_loads_the_saved_session(monkeypatch: pytest.MonkeyPatch, saved: Path) -> None:
    monkeypatch.setattr(launch, "SESSION", Session(saved))
    browser = FakeBrowser()
    page = SessionMarket(browser).create_page()
    assert browser.context_options == [
        {"proxy": None, "storage_state": {"cookies": [LOGIN_COOKIE], "origins": []}}
    ]
    assert page.context.cookie_list == [LOGIN_COOKIE]


@pytest.mark.parametrize("has_session", [False, True])
def test_create_page_without_a_saved_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, has_session: bool
) -> None:
    monkeypatch.setattr(launch, "SESSION", Session(tmp_path / "none.json") if has_session else None)
    browser = FakeBrowser()
    SessionMarket(browser).create_page()
    assert browser.context_options == [{"proxy": None}]


def test_create_page_keeps_proxy_and_reuses_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch, "SESSION", None)
    browser = FakeBrowser()
    market = SessionMarket(browser)
    market.config = SimpleNamespace(
        monitor_config=SimpleNamespace(proxy_server=["a", "b"], get_proxy_options=lambda: "P")
    )
    first = market.create_page()
    assert market.create_page() is first  # no swap requested
    second = market.create_page(swap_proxy=True)  # several proxies: new context
    assert first.closed and second is not first
    assert [o["proxy"] for o in browser.context_options] == ["P", "P"]


def test_create_page_filters_an_old_unfiltered_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    path = tmp_path / "s.json"
    google = {**LOGIN_COOKIE, "name": "SID", "domain": ".google.com"}
    path.write_text(json.dumps({"cookies": [LOGIN_COOKIE, google], "origins": []}))
    monkeypatch.setattr(launch, "SESSION", Session(path))
    browser = FakeBrowser()
    SessionMarket(browser).create_page()
    assert browser.context_options[0]["storage_state"]["cookies"] == [LOGIN_COOKIE]


def test_create_page_without_browser() -> None:
    market = SessionMarket(FakeBrowser())
    market.browser = None  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="no browser"):
        market.create_page()


def test_login_reuses_a_valid_session(
    monkeypatch: pytest.MonkeyPatch, saved: Path, upstream: list[Any], caplog: Any
) -> None:
    monkeypatch.setattr(launch, "SESSION", Session(saved, required=True))
    saved.write_text(saved.read_text())  # touch
    before = saved.stat().st_mtime_ns
    market = SessionMarket(FakeBrowser())
    with caplog.at_level(logging.INFO):
        market.login()
    assert upstream == []  # no login form, no 60-second wait
    assert market.visited == [fb_session.MARKETPLACE_URL]
    assert market.nuc_logged_in
    assert saved.stat().st_mtime_ns >= before
    assert "Using the saved Facebook session" in caplog.text
    assert LOGIN_COOKIE["value"] not in caplog.text


def test_login_falls_back_when_the_session_expired(
    monkeypatch: pytest.MonkeyPatch, saved: Path, upstream: list[Any], caplog: Any
) -> None:
    monkeypatch.setattr(launch, "SESSION", Session(saved))
    market = SessionMarket(FakeBrowser(), logged_in_after_goto=False)
    market.login()
    assert upstream == [market]
    assert "expired" in caplog.text
    # The fresh login is saved for next time.
    state = fb_session.load(saved)
    assert state is not None and state["cookies"] == [LOGIN_COOKIE]
    assert market.nuc_logged_in


def test_required_session_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, saved: Path, upstream: list[Any]
) -> None:
    monkeypatch.setattr(launch, "SESSION", Session(saved, required=True))
    market = SessionMarket(FakeBrowser(), logged_in_after_goto=False)
    with pytest.raises(SessionError, match="require_session"):
        market.login()
    assert upstream == []


def test_required_session_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: list[Any]
) -> None:
    monkeypatch.setattr(launch, "SESSION", Session(tmp_path / "none.json", required=True))
    with pytest.raises(SessionError, match="nuc-hunter login"):
        SessionMarket(FakeBrowser()).login()
    assert upstream == []


def test_first_login_is_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: list[Any]
) -> None:
    path = tmp_path / "s.json"
    monkeypatch.setattr(launch, "SESSION", Session(path))
    market = SessionMarket(FakeBrowser())
    market.login()
    assert upstream == [market]
    assert fb_session.load(path) is not None


def test_no_session_is_plain_upstream_login(
    monkeypatch: pytest.MonkeyPatch, upstream: list[Any]
) -> None:
    monkeypatch.setattr(launch, "SESSION", None)
    market = SessionMarket(FakeBrowser())
    market.login()
    assert upstream == [market]
    assert not getattr(market, "nuc_logged_in", False)


def test_save_failure_does_not_stop_the_monitor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, upstream: list[Any], caplog: Any
) -> None:
    readonly = tmp_path / "ro"
    readonly.mkdir(mode=0o500)
    monkeypatch.setattr(launch, "SESSION", Session(readonly / "s.json"))
    try:
        SessionMarket(FakeBrowser()).login()
    finally:
        readonly.chmod(0o700)
    assert "Couldn't save the Facebook session" in caplog.text


@pytest.fixture
def searching(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    def fake_search(self: Any, item_config: Any) -> Any:
        yield "listing-1"
        yield "listing-2"

    monkeypatch.setattr(launch, "_upstream_search", fake_search)
    return ["listing-1", "listing-2"]


def logged_in_market(monkeypatch: pytest.MonkeyPatch, path: Path, required: bool) -> Any:
    monkeypatch.setattr(launch, "SESSION", Session(path, required=required))
    market = SessionMarket(FakeBrowser())
    market.login()
    assert market.nuc_logged_in
    return market


def test_search_refreshes_the_saved_session(
    monkeypatch: pytest.MonkeyPatch, saved: Path, upstream: list[Any], searching: list[str]
) -> None:
    market = logged_in_market(monkeypatch, saved, required=True)
    new_cookie = {**LOGIN_COOKIE, "name": "xs", "value": "rotated"}
    market.page.context.cookie_list.append(new_cookie)
    assert list(market.search(None)) == searching
    state = fb_session.load(saved)
    assert state is not None and new_cookie in state["cookies"]


def test_search_stops_when_logged_out_and_required(
    monkeypatch: pytest.MonkeyPatch, saved: Path, upstream: list[Any], searching: list[str]
) -> None:
    market = logged_in_market(monkeypatch, saved, required=True)
    market.page.context.cookie_list.clear()
    results = market.search(None)
    # Stops before the first logged-out listing reaches the AI or notifiers.
    with pytest.raises(SessionError, match="logged the monitor out"):
        next(results)


def test_search_with_no_results_still_checks(
    monkeypatch: pytest.MonkeyPatch, saved: Path, upstream: list[Any]
) -> None:
    monkeypatch.setattr(launch, "_upstream_search", lambda self, item: iter(()))
    market = logged_in_market(monkeypatch, saved, required=True)
    market.page.login_form = True  # revoked server-side; c_user still there
    with pytest.raises(SessionError):
        list(market.search(None))


def test_search_warns_once_when_logged_out(
    monkeypatch: pytest.MonkeyPatch,
    saved: Path,
    upstream: list[Any],
    searching: list[str],
    caplog: Any,
) -> None:
    market = logged_in_market(monkeypatch, saved, required=False)
    market.page.context.cookie_list.clear()
    assert list(market.search(None)) == searching
    assert list(market.search(None)) == searching
    assert caplog.text.count("logged the monitor out") == 1
    # The stale session file is left for the next start to report as expired.
    state = fb_session.load(saved)
    assert state is not None and state["cookies"] == [LOGIN_COOKIE]


def test_search_without_login_does_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, searching: list[str]
) -> None:
    path = tmp_path / "s.json"
    monkeypatch.setattr(launch, "SESSION", Session(path, required=True))
    market = SessionMarket(FakeBrowser())
    market.page = market.create_page()  # anonymous page, never logged in
    assert list(market.search(None)) == searching
    assert not path.exists()


class FakeChromium:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.launches: list[dict[str, Any]] = []

    def launch(self, **options: Any) -> str:
        self.launches.append(options)
        if self.error:
            raise self.error
        return "browser"


def fake_monitor(chromium: FakeChromium) -> Any:
    return SimpleNamespace(
        playwright=SimpleNamespace(chromium=chromium), headless=True, logger=None
    )


@pytest.mark.parametrize("sandbox", [True, False])
def test_browser_is_sandboxed_without_secrets(
    monkeypatch: pytest.MonkeyPatch, sandbox: bool
) -> None:
    monkeypatch.setattr(launch, "SANDBOX", sandbox)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("FACEBOOK_PASSWORD", "hunter2")
    chromium = FakeChromium()
    assert launch.launch_browser(fake_monitor(chromium)) == "browser"
    [options] = chromium.launches
    assert options["headless"] is True and options["chromium_sandbox"] is sandbox
    assert "ANTHROPIC_API_KEY" not in options["env"]
    assert "FACEBOOK_PASSWORD" not in options["env"]
    assert options["env"]["PATH"] == os.environ["PATH"]


def test_browser_launch_failure_explains(monkeypatch: pytest.MonkeyPatch) -> None:
    from playwright.sync_api import Error

    monkeypatch.setattr(launch, "SANDBOX", True)
    chromium = FakeChromium(Error("Executable doesn't exist at /x\nlong call log"))
    with pytest.raises(RuntimeError, match="browser_sandbox = false") as e:
        launch.launch_browser(fake_monitor(chromium))
    assert "playwright install chromium" in str(e.value)
    assert "call log" not in str(e.value)


def test_login_detects_a_revoked_session(
    monkeypatch: pytest.MonkeyPatch, saved: Path, upstream: list[Any]
) -> None:
    monkeypatch.setattr(launch, "SESSION", Session(saved, required=True))
    market = SessionMarket(FakeBrowser())
    real_goto = market.goto_url

    def goto_shows_login_form(url: str, attempt: int = 0) -> None:
        real_goto(url)
        market.page.login_form = True  # c_user cookie kept, but logged out

    market.goto_url = goto_shows_login_form  # type: ignore[method-assign]
    with pytest.raises(SessionError, match="require_session"):
        market.login()


def test_failed_backup_exclusion_warns_once(
    monkeypatch: pytest.MonkeyPatch, saved: Path, upstream: list[Any], caplog: Any
) -> None:
    monkeypatch.setattr(fb_session, "exclude_from_backups", lambda path: False)
    market = logged_in_market(monkeypatch, saved, required=False)
    launch._save(market, launch.SESSION)  # type: ignore[arg-type]
    assert caplog.text.count("Time Machine") == 1
