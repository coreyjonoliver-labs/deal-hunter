"""Save a Facebook login once and reuse it, instead of logging in on every start.

``deal-hunter login`` opens a browser, waits for you to log in (2FA and
checkpoints included), and saves the browser's cookies and local storage with
Playwright's ``storage_state``. ``deal-hunter run`` loads that file into the
monitor's browser (see launch.py).

The file is a full login to the account: anyone who has it is logged in as
you. It is written owner-only, only Facebook's cookies and storage are kept
(not those of any other site visited in the same window), and its contents
are never printed or logged.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from deal_hunter.files import write_private

FACEBOOK = "https://www.facebook.com"
LOGIN_URL = "https://www.facebook.com/login/"
MARKETPLACE_URL = "https://www.facebook.com/marketplace/"
AIMM_HOME = Path.home() / ".ai-marketplace-monitor"
LOGIN_TIMEOUT_S = 300
NAV_TIMEOUT_MS = 60_000

# Pages Facebook shows while a login is unfinished: the form, 2FA, security
# checkpoints and "save this browser?". The c_user cookie can exist on these.
_NOT_YET = ("/login", "/checkpoint", "/two_step_verification", "/two_factor", "/recover")


class SessionError(RuntimeError):
    """The saved Facebook session is missing, unreadable or no longer logged in."""


@dataclass(frozen=True)
class Session:
    path: Path
    required: bool = False


# Browser environment allow-list: the browser renders untrusted pages, so it
# doesn't get the API keys and tokens in deal-hunter's environment.
_BROWSER_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "TZ", "DISPLAY", "WAYLAND_DISPLAY")
_BROWSER_ENV_PREFIXES = ("LC_", "XDG_", "FONTCONFIG_")


def browser_env() -> dict[str, str | float | bool]:
    """The environment to start a browser with: locale, display and paths only."""
    return {
        k: v
        for k, v in os.environ.items()
        if k in _BROWSER_ENV or k.startswith(_BROWSER_ENV_PREFIXES)
    }


def logged_in(page: Any) -> bool:
    """True if page shows a finished Facebook login.

    Needs all three: not a login/2FA/checkpoint page, a c_user cookie, and no
    password box (logged-out Facebook pages show a login form; a session that
    was revoked server-side can leave c_user behind).
    """
    if urlparse(page.url).path.startswith(_NOT_YET):
        return False
    if not any(
        c.get("name") == "c_user" and c.get("value") for c in page.context.cookies(FACEBOOK)
    ):
        return False
    return page.query_selector('input[name="pass"]') is None


def _is_facebook(host: str | None) -> bool:
    host = (host or "").lstrip(".").lower()
    return host == "facebook.com" or host.endswith(".facebook.com")


def facebook_only(state: dict[str, Any]) -> dict[str, Any]:
    """state with only Facebook's cookies and local storage."""
    return {
        "cookies": [c for c in state.get("cookies", []) if _is_facebook(c.get("domain"))],
        "origins": [
            o
            for o in state.get("origins", [])
            if _is_facebook(urlparse(o.get("origin", "")).hostname)
        ],
    }


def load(path: Path) -> dict[str, Any] | None:
    """The saved storage state, or None if there is none. Raises SessionError if unreadable."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SessionError(f"can't read {path}: {e.strerror}") from e
    try:
        state = json.loads(text)
    except ValueError:
        state = None
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        # Don't echo the contents: they're credentials.
        raise SessionError(
            f"{path} is not a saved session (damaged?). Delete it, then run `deal-hunter login`."
        )
    return state


def save(context: Any, path: Path) -> bool:
    """Write context's Facebook cookies and local storage to path, owner-only.

    Refuses to replace a file that isn't a saved session, so a wrong
    session_file can't overwrite something else. Returns False if it couldn't
    be excluded from Time Machine.
    """
    state = facebook_only(context.storage_state())
    _private_dir(path.parent)
    check_writable(path)
    write_private(path, json.dumps(state))
    _private_dir(AIMM_HOME)  # may not exist yet when the session lives elsewhere
    write_private(meta_path(path), json.dumps(_metadata(state)))
    # Each save is a new file, so the backup exclusion is set again each time.
    return exclude_from_backups(path)


def copy_seed(seed: Path, path: Path) -> bool:
    """Copy a saved session from seed to path if seed changed since the last copy.

    For containers, where the session comes in as a read-only file (a mounted
    secret) and the monitor keeps refreshing its own copy at path: the same
    seed isn't copied again over the refreshed copy, a new one is. Returns
    True if it copied. A missing or empty seed is not an error.
    """
    try:
        text = seed.read_text()
    except FileNotFoundError:
        return False
    except OSError as e:
        raise SessionError(f"can't read the session seed {seed}: {e.strerror}") from e
    if not text.strip():
        return False
    digest = hashlib.sha256(text.encode()).hexdigest()
    mark = seed_mark_path(path)
    with contextlib.suppress(OSError):
        if mark.read_text().strip() == digest:
            return False
    try:
        state = json.loads(text)
    except ValueError:
        state = None
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise SessionError(f"the session seed {seed} is not a saved session.")  # no contents
    state = facebook_only(state)
    _private_dir(path.parent)
    check_writable(path)
    write_private(path, json.dumps(state))
    write_private(mark, digest + "\n")
    _private_dir(AIMM_HOME)
    write_private(meta_path(path), json.dumps(_metadata(state)))
    return True


def seed_mark_path(path: Path) -> Path:
    """Where copy_seed records which seed it last copied (its sha256)."""
    return path.with_name(f".{path.name}.seed-sha256")


def meta_path(path: Path) -> Path:
    """Where save() records when it saved, for `deal-hunter web` (which never reads path).

    Always in aimm's home, wherever the session itself is, so the session can
    live somewhere `web` can't read (in a pod, a volume only the monitor mounts).
    """
    return AIMM_HOME / f"{path.stem}.meta.json"


def _metadata(state: dict[str, Any]) -> dict[str, Any]:
    expires = None
    for cookie in state.get("cookies", []):
        if cookie.get("name") == "c_user" and isinstance(cookie.get("expires"), (int, float)):
            expires = cookie["expires"] if cookie["expires"] > 0 else None
    return {"saved_at": time.time(), "expires": expires}


def read_metadata(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(meta_path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_writable(path: Path) -> None:
    """Raise SessionError if path exists and isn't a saved session (don't overwrite it)."""
    if not path.exists():
        return
    try:
        load(path)
    except SessionError:
        raise SessionError(
            f"{path} exists and isn't a saved session; not overwriting it. "
            "Delete it if it's a damaged session, or change facebook.session_file."
        ) from None


def private_home(directory: Path | None = None) -> None:
    """Make aimm's home (log, cache, session) owner-only. aimm creates it 0755."""
    directory = directory or AIMM_HOME
    if not directory.is_dir() or directory.stat().st_uid != os.getuid():
        return
    directory.chmod(0o700)
    for child in directory.iterdir():
        st = child.lstat()
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
            child.chmod(0o600)


def _private_dir(directory: Path) -> None:
    """Create directory 0700. Existing directories are left alone (the file is 0600),
    except aimm's home, which is tightened, and ones others can write to, refused."""
    created = not directory.exists()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = directory.stat()
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not st.st_mode & stat.S_ISVTX:
        # Others could replace the file with their own session.
        raise SessionError(f"{directory} is writable by other users; not saving there.")
    if (created or directory == AIMM_HOME) and st.st_uid == os.getuid():
        directory.chmod(0o700)


def exclude_from_backups(path: Path) -> bool:
    """macOS: keep path out of Time Machine. True if excluded (or not a Mac).

    A backed-up copy stays a valid login until it's revoked. The mark is on
    the file, so it has to be set again whenever the file is replaced.
    """
    if sys.platform != "darwin":
        return True
    try:
        subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["/usr/bin/tmutil", "addexclusion", str(path)], check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def interactive_login(path: Path, timeout_s: float = LOGIN_TIMEOUT_S) -> None:
    """Open a browser, wait until you've logged in to Facebook, then save the session."""
    from playwright.sync_api import Error, sync_playwright

    check_writable(path)  # before the login, not after it
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=False, chromium_sandbox=True, env=browser_env())
        except Error as e:
            raise SessionError(
                f"couldn't start Chromium ({first_line(e)}). "
                "Run `uv run playwright install chromium`."
            ) from None
        try:
            page = browser.new_context().new_page()
            page.goto(LOGIN_URL, timeout=NAV_TIMEOUT_MS)
            deadline = time.monotonic() + timeout_s
            while not _settled_login(page, Error):
                if time.monotonic() > deadline:
                    raise SessionError(
                        f"not logged in after {int(timeout_s)} seconds; nothing saved."
                    )
                page.wait_for_timeout(1000)
            # Load a normal page so Facebook finishes setting its cookies.
            page.goto(MARKETPLACE_URL, timeout=NAV_TIMEOUT_MS)
            if not logged_in(page):
                raise SessionError("Facebook logged the browser out again; nothing saved.")
            save(page.context, path)
        except Error as e:
            # First line only: Playwright's call log can include login URLs
            # with one-time tokens.
            raise SessionError(
                f"browser closed or failed before the login finished: {first_line(e)}"
            ) from None
        finally:
            with contextlib.suppress(Error):  # already closed by the user
                browser.close()


def _settled_login(page: Any, error: type[Exception]) -> bool:
    try:
        return logged_in(page)
    except error as e:  # the page navigated mid-check
        if "closed" in str(e):
            raise
        return False


def first_line(e: Exception) -> str:
    message = getattr(e, "message", None) or str(e)
    return message.strip().splitlines()[0] if message.strip() else type(e).__name__
