"""deal-hunter command line: init, render, check, login, run, web."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from importlib import resources
from pathlib import Path

from deal_hunter.files import write_private
from deal_hunter.render import build_config, render, required_env
from deal_hunter.session import (
    LOGIN_TIMEOUT_S,
    Session,
    SessionError,
    copy_seed,
    exclude_from_backups,
    first_line,
    interactive_login,
)
from deal_hunter.session import load as load_session
from deal_hunter.settings import Settings, SettingsError, load_settings

DEFAULT_SETTINGS = Path("deal-hunter.toml")
# The settings file's name before the project was renamed.
LEGACY_SETTINGS = Path("nuc-hunter.toml")
DEFAULT_OUTPUT = Path("build/aimm-config.toml")
AIMM_HOME_CONFIG = Path.home() / ".ai-marketplace-monitor" / "config.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deal-hunter",
        description="NUC / mini-PC tuned front-end for ai-marketplace-monitor.",
    )
    parser.add_argument(
        "-s", "--settings", type=Path, default=DEFAULT_SETTINGS, help="settings file"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create deal-hunter.toml from the example")

    p_render = sub.add_parser("render", help="write the ai-marketplace-monitor config")
    p_render.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)

    p_check = sub.add_parser("check", help="test a listing against the keyword/price filters")
    p_check.add_argument("title")
    p_check.add_argument("description", nargs="?", default="")
    p_check.add_argument("--price", type=int, default=None)

    sub.add_parser("login", help="log in to Facebook once and save the session for `run`")

    p_run = sub.add_parser("run", help="render, then start ai-marketplace-monitor")
    p_run.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    p_run.add_argument(
        "--webui",
        action="store_true",
        help="enable aimm's web UI (unauthenticated on 127.0.0.1; off by default)",
    )
    p_run.add_argument(
        "--session-seed",
        type=Path,
        metavar="FILE",
        help="copy this saved session to facebook.session_file whenever its content "
        "changes (e.g. a mounted secret in a container)",
    )
    p_run.add_argument(
        "--wait-for-session",
        type=int,
        default=0,
        metavar="SECONDS",
        help="when the saved session is missing or expired, wait this long and start "
        "again instead of exiting (needs facebook.require_session)",
    )
    p_run.add_argument(
        "--restart-after",
        type=int,
        default=0,
        metavar="SECONDS",
        help="when the monitor stops on any other error, wait this long (doubling on each "
        "consecutive failure, up to 6 hours) and start again instead of exiting",
    )
    p_run.add_argument("aimm_args", nargs=argparse.REMAINDER, help="extra args for aimm, after --")

    p_web = sub.add_parser("web", help="serve a page with results and monitor status")
    p_web.add_argument("--host", default="127.0.0.1", help="address to listen on")
    p_web.add_argument("--port", type=int, default=8740)
    p_web.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="NAME",
        help="extra Host name to answer to, e.g. behind a reverse proxy (repeatable)",
    )

    args = parser.parse_args(argv)
    if (
        args.command != "init"
        and args.settings == DEFAULT_SETTINGS
        and not DEFAULT_SETTINGS.exists()
        and LEGACY_SETTINGS.is_file()
    ):
        print(f"Using {LEGACY_SETTINGS}; rename it to {DEFAULT_SETTINGS}.", file=sys.stderr)
        args.settings = LEGACY_SETTINGS
    # In a container this process is PID 1, which ignores SIGTERM unless it
    # has a handler; stop the way Ctrl+C does. (main runs on the main thread.)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        if args.command == "init":
            return _init(args.settings)
        if args.command == "render":
            return _render(args.settings, args.output)
        if args.command == "check":
            return _check(args.settings, args.title, args.description, args.price)
        if args.command == "login":
            return _login(args.settings)
        if args.command == "web":
            return _web(args.settings, args.host, args.port, args.allow_host)
        return _run(
            args.settings,
            args.output,
            args.aimm_args,
            webui=args.webui,
            seed=args.session_seed,
            wait_s=args.wait_for_session,
            restart_s=args.restart_after,
        )
    except (SettingsError, SessionError) as e:
        print(f"deal-hunter: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _init(path: Path) -> int:
    if path.exists():
        print(f"{path} already exists; leaving it alone.", file=sys.stderr)
        return 1
    example = resources.files("deal_hunter").joinpath("example-settings.toml").read_text()
    write_private(path, example)
    print(f"Wrote {path}. Set location.search_city, then run `deal-hunter run`.")
    return 0


def _render(settings_path: Path, output: Path) -> int:
    settings = load_settings(settings_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Owner-only: the file holds no secrets, but it does hold your location.
    write_private(output, render(settings))
    print(f"Wrote {output}")
    return 0


def _check(settings_path: Path, title: str, description: str, price: int | None) -> int:
    from deal_hunter.filters import drop_reasons

    settings = load_settings(settings_path)
    text = f"{title}  {description}"
    for name, item in build_config(settings)["item"].items():
        reasons = drop_reasons(item, text, price)
        verdict = "PASS (goes to AI)" if not reasons else "DROP: " + "; ".join(reasons)
        print(f"[{name}] {verdict}")
    return 0


def _login(settings_path: Path) -> int:
    settings = load_settings(settings_path)
    if settings.anonymous:
        raise SettingsError("facebook.anonymous is true, so `run` wouldn't use a login.")
    print(
        f"Log in to Facebook in the browser window (you have {LOGIN_TIMEOUT_S // 60} minutes, "
        "including any 2FA or security check).",
        flush=True,
    )
    interactive_login(settings.session_file)
    print(f"Saved the session to {settings.session_file}. Anyone with this file is logged in")
    print("as you: don't commit or share it. `deal-hunter run` now uses it.")
    if not exclude_from_backups(settings.session_file):
        print(
            f"warning: couldn't exclude {settings.session_file} from Time Machine backups.",
            file=sys.stderr,
        )
    return 0


def _web(settings_path: Path, host: str, port: int, allow_hosts: list[str]) -> int:
    from deal_hunter.dashboard import LOOPBACK, serve
    from deal_hunter.session import AIMM_HOME

    settings = load_settings(settings_path)
    if host not in LOOPBACK:
        print(
            f"warning: serving on {host}. The page has no login: anyone who can reach it "
            "sees your results, search location and API spend. Put it behind an "
            "authenticating proxy.",
            file=sys.stderr,
        )
    try:
        serve(settings, AIMM_HOME, host, port, allow_hosts)
    except OSError as e:
        print(
            f"deal-hunter: can't listen on {host}:{port}: {e.strerror}. Try --port.",
            file=sys.stderr,
        )
        return 1
    return 0


# State carried across a restart (os.execv keeps the environment).
_AWAIT_SEED = "DEAL_HUNTER_AWAITING_SEED"  # the session failed; wait for a new seed
_RESTARTS = "DEAL_HUNTER_RESTARTS"  # consecutive failed runs, for the backoff
MAX_RESTART_DELAY_S = 6 * 3600
# A run this long counts as having worked: the backoff starts over.
HEALTHY_RUN_S = 3600


def _run(
    settings_path: Path,
    output: Path,
    aimm_args: list[str],
    *,
    webui: bool,
    seed: Path | None = None,
    wait_s: int = 0,
    restart_s: int = 0,
) -> int:
    settings = load_settings(settings_path)
    missing = [name for name in required_env(settings) if not os.environ.get(name)]
    if missing:
        raise SettingsError(
            f"missing environment variable(s): {', '.join(missing)}. "
            "Export them (see README) or change [ai]/[notify] in your settings."
        )
    if (seed is not None or wait_s) and not settings.require_session:
        raise SettingsError(
            "--session-seed and --wait-for-session need facebook.require_session = true."
        )
    if wait_s < 0 or restart_s < 0:
        raise SettingsError("--wait-for-session and --restart-after can't be negative.")
    awaiting = os.environ.pop(_AWAIT_SEED, "") == "1" and seed is not None
    try:
        restarts = max(0, int(os.environ.pop(_RESTARTS, "0")))
    except ValueError:
        restarts = 0
    started = time.monotonic()
    try:
        copied = seed is not None and copy_seed(seed, settings.session_file)
        if copied:
            print(f"Copied a new session from {seed} to {settings.session_file}.", flush=True)
        if not (awaiting and not copied):
            return _start(settings_path, settings, output, aimm_args, webui=webui)
        # The session failed and the seed hasn't changed: don't load Facebook
        # again with dead cookies, just keep waiting for a new seed.
        print(f"deal-hunter: still waiting for a new session in {seed}.", file=sys.stderr)
        time.sleep(wait_s)
        os.environ[_AWAIT_SEED] = "1"
    except SessionError as e:
        if not wait_s:
            raise
        _record(
            f"Waiting for a Facebook session ({e}). "
            + (
                f"Checking {seed} for a new one every {_minutes(wait_s)}."
                if seed is not None
                else f"Trying again in {_minutes(wait_s)}."
            )
        )
        time.sleep(wait_s)
        if seed is not None:
            os.environ[_AWAIT_SEED] = "1"
    except (SettingsError, KeyboardInterrupt):
        raise
    except Exception as e:  # noqa: BLE001 — anything else aimm stops on
        if not restart_s:
            raise
        if time.monotonic() - started > HEALTHY_RUN_S:
            restarts = 0
        delay = min(restart_s * 2**restarts, MAX_RESTART_DELAY_S)
        _record(f"The monitor stopped ({first_line(e)}). Starting again in {_minutes(delay)}.")
        time.sleep(delay)
        os.environ[_RESTARTS] = str(restarts + 1)
    _restart()
    return 0  # pragma: no cover


def _restart() -> None:
    """Run the same command again in a fresh process (same PID).

    aimm keeps state (its cache, logging, the browser) at module level, so it
    can't be restarted inside this process. This re-runs the process's own
    command line, not main()'s argv: it's for the command-line entry point.
    """
    os.execv(sys.executable, sys.orig_argv)  # noqa: S606 — the same command again


def _record(message: str) -> None:
    """Print a problem and record it where `web` shows recent problems."""
    from deal_hunter.events import EventRecorder, events_path
    from deal_hunter.session import AIMM_HOME

    print(f"deal-hunter: {message}", file=sys.stderr, flush=True)
    try:
        AIMM_HOME.mkdir(mode=0o700, exist_ok=True)
        recorder = EventRecorder(events_path(AIMM_HOME, migrate=True))
        recorder.write({"ts": time.time(), "kind": "error", "message": recorder.redact(message)})
    except OSError:
        pass


def _minutes(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def _start(
    settings_path: Path, settings: Settings, output: Path, aimm_args: list[str], *, webui: bool
) -> int:
    session = (
        None if settings.anonymous else Session(settings.session_file, settings.require_session)
    )
    # Fail before starting a browser: SessionError if the file is unreadable.
    if session is not None and load_session(session.path) is None:
        if session.required:
            raise SessionError(
                f"{session.path} doesn't exist and facebook.require_session is set. "
                "Run `deal-hunter login` to create it."
            )
    _render(settings_path, output)

    if aimm_args and aimm_args[0] == "--":
        aimm_args = aimm_args[1:]
    if AIMM_HOME_CONFIG.exists():
        print(
            f"warning: {AIMM_HOME_CONFIG} exists and will be merged into this run; "
            "its items, AI keys and notifiers apply too.",
            file=sys.stderr,
        )
    # aimm's web UI is on by default and unauthenticated on loopback: any web
    # page could read its log stream, and via DNS rebinding edit the config to
    # send ${API_KEY} values elsewhere. Keep it off unless asked for.
    argv = ["--config", str(output), "--webui" if webui else "--no-webui"]
    if settings.anonymous or settings.require_session:  # no one is there to log in
        argv.append("--headless")
    argv += aimm_args
    print("Starting: ai-marketplace-monitor", " ".join(argv), flush=True)
    # In this process, not `python -m`: that would put the current directory
    # first on sys.path. launch.main applies deal-hunter's fixes to aimm.
    from deal_hunter.launch import main as run_aimm

    run_aimm(argv, session, settings.browser_sandbox)
    return 0  # pragma: no cover — aimm exits via SystemExit
