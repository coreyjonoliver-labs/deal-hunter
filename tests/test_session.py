"""Saving, loading and checking a Facebook session."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from fakes import LOGIN_COOKIE, FakeBrowserPage, FakeContext

from nuc_hunter import session

REAL_EXCLUDE = session.exclude_from_backups  # conftest stubs it out for every test


def page(url: str, cookies: list[dict[str, Any]]) -> FakeBrowserPage:
    return FakeBrowserPage(FakeContext(cookies), url)


@pytest.mark.parametrize(
    ("url", "cookies", "expected"),
    [
        ("https://www.facebook.com/marketplace/", [LOGIN_COOKIE], True),
        ("https://www.facebook.com/", [LOGIN_COOKIE], True),
        ("https://www.facebook.com/marketplace/", [], False),
        ("https://www.facebook.com/marketplace/", [{**LOGIN_COOKIE, "value": ""}], False),
        ("https://www.facebook.com/checkpoint/1501092823525282/", [LOGIN_COOKIE], False),
        ("https://www.facebook.com/two_step_verification/two_factor/", [LOGIN_COOKIE], False),
        ("https://www.facebook.com/login/save-device/", [LOGIN_COOKIE], False),
        ("https://www.facebook.com/login/?next=x", [LOGIN_COOKIE], False),
    ],
)
def test_logged_in(url: str, cookies: list[dict[str, Any]], expected: bool) -> None:
    p = page(url, cookies)
    assert session.logged_in(p) is expected


def test_logged_in_only_reads_facebook_cookies() -> None:
    p = page("https://www.facebook.com/", [LOGIN_COOKIE])
    session.logged_in(p)
    assert p.context.urls == [session.FACEBOOK]


def test_save_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "new-dir" / "s.json"
    session.save(FakeContext([LOGIN_COOKIE]), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["cookies"] == [LOGIN_COOKIE]


def test_save_does_not_follow_symlink(tmp_path: Path) -> None:
    target = tmp_path / "victim.json"
    session.save(FakeContext([]), target)
    before = target.read_text()
    path = tmp_path / "s.json"
    path.symlink_to(target)
    session.save(FakeContext([LOGIN_COOKIE]), path)
    assert target.read_text() == before
    assert not path.is_symlink()


@pytest.mark.parametrize("text", ["ssh-ed25519 AAAA...", "{}"])
def test_save_wont_overwrite_other_files(tmp_path: Path, text: str) -> None:
    path = tmp_path / "id_ed25519"
    path.write_text(text)
    with pytest.raises(session.SessionError, match="not overwriting"):
        session.save(FakeContext([LOGIN_COOKIE]), path)
    assert path.read_text() == text


def test_save_keeps_only_facebook() -> None:
    state = {
        "cookies": [
            LOGIN_COOKIE,
            {**LOGIN_COOKIE, "name": "xs", "domain": "www.facebook.com"},
            {**LOGIN_COOKIE, "name": "SID", "domain": ".google.com"},
            {**LOGIN_COOKIE, "name": "evil", "domain": ".notfacebook.com"},
            {**LOGIN_COOKIE, "name": "evil2", "domain": "facebook.com.evil.example"},
        ],
        "origins": [
            {"origin": "https://www.facebook.com", "localStorage": [{"name": "a", "value": "1"}]},
            {"origin": "https://mail.google.com", "localStorage": [{"name": "b", "value": "2"}]},
        ],
    }
    kept = session.facebook_only(state)
    assert [c["name"] for c in kept["cookies"]] == ["c_user", "xs"]
    assert [o["origin"] for o in kept["origins"]] == ["https://www.facebook.com"]


def test_save_filters_what_it_writes(tmp_path: Path) -> None:
    google = {**LOGIN_COOKIE, "name": "SID", "domain": ".google.com"}
    path = tmp_path / "s.json"
    session.save(FakeContext([LOGIN_COOKIE, google]), path)
    assert "google" not in path.read_text()


def test_save_leaves_an_existing_directory_alone(tmp_path: Path) -> None:
    directory = tmp_path / "home"  # e.g. session_file = "~/fb.json"
    directory.mkdir()
    directory.chmod(0o755)
    session.save(FakeContext([LOGIN_COOKIE]), directory / "s.json")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert stat.S_IMODE((directory / "s.json").stat().st_mode) == 0o600


def test_save_tightens_aimm_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / ".ai-marketplace-monitor"
    home.mkdir()
    home.chmod(0o755)
    monkeypatch.setattr(session, "AIMM_HOME", home)
    session.save(FakeContext([LOGIN_COOKIE]), home / "s.json")
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_private_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / ".ai-marketplace-monitor"
    home.mkdir()
    home.chmod(0o755)
    log = home / "ai-marketplace-monitor.log"
    log.write_text("searching near Springfield")
    log.chmod(0o644)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("")
    elsewhere.chmod(0o644)
    (home / "link").symlink_to(elsewhere)
    monkeypatch.setattr(session, "AIMM_HOME", home)
    session.private_home()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o644  # symlinks not followed
    session.private_home(tmp_path / "missing")  # no error


def test_every_save_excludes_from_backups(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Each save replaces the file, which drops a per-file Time Machine mark.
    excluded: list[Path] = []
    monkeypatch.setattr(session, "exclude_from_backups", excluded.append)
    path = tmp_path / "s.json"
    session.save(FakeContext([LOGIN_COOKIE]), path)
    session.save(FakeContext([LOGIN_COOKIE]), path)
    assert excluded == [path, path]


@pytest.mark.parametrize(
    ("platform", "fails", "expected", "ran"),
    [("darwin", False, True, True), ("darwin", True, False, True), ("linux", False, True, False)],
)
def test_exclude_from_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    fails: bool,
    expected: bool,
    ran: bool,
) -> None:
    import subprocess

    argvs: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> None:
        argvs.append(argv)
        if fails:
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(session.sys, "platform", platform)
    monkeypatch.setattr(session.subprocess, "run", fake_run)
    assert REAL_EXCLUDE(tmp_path / "s.json") is expected
    assert argvs == ([["/usr/bin/tmutil", "addexclusion", str(tmp_path / "s.json")]] if ran else [])


def test_browser_env_drops_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_secret")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    env = session.browser_env()
    assert "sk-secret" not in env.values() and "ops_secret" not in env.values()
    assert env["LC_ALL"] == "en_US.UTF-8" and "PATH" in env


def test_logged_out_page_with_stale_cookie() -> None:
    p = page("https://www.facebook.com/marketplace/", [LOGIN_COOKIE])
    p.login_form = True
    assert not session.logged_in(p)


def test_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    assert session.load(path) is None
    session.save(FakeContext([LOGIN_COOKIE]), path)
    state = session.load(path)
    assert state is not None and state["cookies"] == [LOGIN_COOKIE]


@pytest.mark.parametrize("text", ["not json c_user=SECRET", "[]", '{"cookies": "SECRET"}', ""])
def test_load_rejects_junk_without_echoing_it(tmp_path: Path, text: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(text)
    with pytest.raises(session.SessionError, match="nuc-hunter login") as e:
        session.load(path)
    assert "SECRET" not in str(e.value)


def test_load_unreadable(tmp_path: Path) -> None:
    with pytest.raises(session.SessionError, match="can't read"):
        session.load(tmp_path)  # a directory


class ScriptedPage(FakeBrowserPage):
    """Logs in after `polls` waits, the way a person would."""

    def __init__(self, polls: int) -> None:
        super().__init__(FakeContext(), "about:blank")
        self.polls = polls
        self.visited: list[str] = []

    def goto(self, url: str, *, timeout: float) -> None:
        assert timeout > 0
        self.visited.append(url)
        self.url = url

    def wait_for_timeout(self, ms: float) -> None:
        self.polls -= 1
        if self.polls == 0:
            self.url = "https://www.facebook.com/"
            self.context.cookie_list.append(LOGIN_COOKIE)


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import playwright.sync_api

    state: dict[str, Any] = {"page": ScriptedPage(polls=3), "closed": False, "headless": None}

    class Browser:
        def new_context(self) -> Any:
            class Ctx:
                def new_page(_self) -> ScriptedPage:
                    return state["page"]

            return Ctx()

        def close(self) -> None:
            state["closed"] = True

    class PW:
        class chromium:  # noqa: N801
            @staticmethod
            def launch(**options: Any) -> Browser:
                state["options"] = options
                state["headless"] = options["headless"]
                return Browser()

        def __enter__(self) -> PW:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", PW)
    return state


def test_interactive_login_saves_once_logged_in(
    tmp_path: Path, fake_playwright: dict[str, Any]
) -> None:
    path = tmp_path / "s.json"
    session.interactive_login(path)
    assert fake_playwright["headless"] is False
    assert fake_playwright["options"]["chromium_sandbox"] is True
    assert "ANTHROPIC_API_KEY" not in fake_playwright["options"]["env"]
    assert fake_playwright["closed"]
    assert fake_playwright["page"].visited == [session.LOGIN_URL, session.MARKETPLACE_URL]
    state = session.load(path)
    assert state is not None and state["cookies"] == [LOGIN_COOKIE]


def test_interactive_login_times_out_without_saving(
    tmp_path: Path, fake_playwright: dict[str, Any]
) -> None:
    fake_playwright["page"] = ScriptedPage(polls=10**9)
    path = tmp_path / "s.json"
    with pytest.raises(session.SessionError, match="nothing saved"):
        session.interactive_login(path, timeout_s=0)
    assert not path.exists()
    assert fake_playwright["closed"]


def test_interactive_login_browser_closed(tmp_path: Path, fake_playwright: dict[str, Any]) -> None:
    from playwright.sync_api import Error

    class ClosedPage(ScriptedPage):
        def wait_for_timeout(self, ms: float) -> None:
            raise Error("Target page, context or browser has been closed")

    fake_playwright["page"] = ClosedPage(polls=1)
    with pytest.raises(session.SessionError, match="browser closed"):
        session.interactive_login(tmp_path / "s.json")
    assert not (tmp_path / "s.json").exists()


def test_interactive_login_checks_the_target_first(
    tmp_path: Path, fake_playwright: dict[str, Any]
) -> None:
    path = tmp_path / "s.json"
    path.write_text("damaged")
    with pytest.raises(session.SessionError, match="Delete it"):
        session.interactive_login(path)
    assert "options" not in fake_playwright  # no browser, no wasted login


def test_interactive_login_without_chromium(
    tmp_path: Path, fake_playwright: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import playwright.sync_api
    from playwright.sync_api import Error

    pw = playwright.sync_api.sync_playwright

    def no_browser(**options: Any) -> Any:
        raise Error("Executable doesn't exist\ncall log with secrets")

    monkeypatch.setattr(pw.chromium, "launch", staticmethod(no_browser))
    with pytest.raises(session.SessionError, match="playwright install chromium") as e:
        session.interactive_login(tmp_path / "s.json")
    assert "call log" not in str(e.value)


def test_interactive_login_survives_mid_navigation_checks(
    tmp_path: Path, fake_playwright: dict[str, Any]
) -> None:
    from playwright.sync_api import Error

    class NavigatingPage(ScriptedPage):
        def query_selector(self, selector: str) -> object | None:
            if self.polls == 1:
                raise Error("Execution context was destroyed, most likely because of a navigation")
            return super().query_selector(selector)

    fake_playwright["page"] = NavigatingPage(polls=2)
    # Logged in (cookie + URL) one poll early, mid-navigation: must keep polling.
    fake_playwright["page"].url = "https://www.facebook.com/"
    fake_playwright["page"].context.cookie_list.append(LOGIN_COOKIE)
    session.interactive_login(tmp_path / "s.json")
    assert session.load(tmp_path / "s.json") is not None


@pytest.mark.parametrize(("mode", "ok"), [(0o777, False), (0o775, False), (0o1777, True)])
def test_save_refuses_others_writable_dirs(tmp_path: Path, mode: int, ok: bool) -> None:
    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(mode)
    if ok:  # sticky, like /tmp: others can't replace your file
        session.save(FakeContext([LOGIN_COOKIE]), directory / "s.json")
    else:
        with pytest.raises(session.SessionError, match="writable by other users"):
            session.save(FakeContext([LOGIN_COOKIE]), directory / "s.json")


def test_copy_seed_ignores_missing_empty_and_rejects_junk(tmp_path: Path) -> None:
    fb = tmp_path / "fb.json"
    assert not session.copy_seed(tmp_path / "nope.json", fb)
    empty = tmp_path / "empty.json"
    empty.write_text("  \n")
    assert not session.copy_seed(empty, fb)
    junk = tmp_path / "junk.json"
    junk.write_text("secret-cookie-value")
    with pytest.raises(session.SessionError) as e:
        session.copy_seed(junk, fb)
    assert "secret-cookie-value" not in str(e.value)
    assert not fb.exists()


def test_copy_seed_writes_owner_only_with_metadata(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps({"cookies": [LOGIN_COOKIE, {"name": "x", "domain": ".evil.example"}]})
    )
    fb = tmp_path / "dir" / "fb.json"
    assert session.copy_seed(seed, fb)
    assert [c["name"] for c in session.load(fb)["cookies"]] == ["c_user"]  # Facebook only
    assert stat.S_IMODE(fb.stat().st_mode) == 0o600
    assert session.read_metadata(fb) is not None
    assert not session.copy_seed(seed, fb)  # unchanged seed: not copied again
