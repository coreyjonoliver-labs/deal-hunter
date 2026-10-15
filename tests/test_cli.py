from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from deal_hunter.cli import main

SETTINGS = """
[location]
search_city = "seattle"
[notify]
methods = ["ntfy"]
"""


@pytest.fixture
def settings(tmp_path: Path) -> Path:
    p = tmp_path / "deal-hunter.toml"
    p.write_text(SETTINGS)
    return p


def test_init_writes_once(tmp_path: Path) -> None:
    p = tmp_path / "s.toml"
    assert main(["-s", str(p), "init"]) == 0
    assert "search_city" in p.read_text()
    assert main(["-s", str(p), "init"]) == 1


def test_render_is_owner_only(settings: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "c.toml"
    out.parent.mkdir()
    out.write_text("old")
    out.chmod(0o644)  # pre-existing file with a looser mode gets replaced
    assert main(["-s", str(settings), "render", "-o", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "[item.nuc]" in out.read_text()


def test_render_does_not_follow_symlink(settings: Path, tmp_path: Path) -> None:
    target = tmp_path / "victim.txt"
    target.write_text("keep me")
    out = tmp_path / "c.toml"
    out.symlink_to(target)
    assert main(["-s", str(settings), "render", "-o", str(out)]) == 0
    assert target.read_text() == "keep me"
    assert not out.is_symlink()


def test_init_is_owner_only(tmp_path: Path) -> None:
    p = tmp_path / "s.toml"
    main(["-s", str(p), "init"])
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_run_disables_webui_by_default(
    settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("NTFY_TOPIC", "t")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "deal_hunter.launch.main", lambda argv, session, sandbox: calls.append(argv)
    )
    out = str(tmp_path / "c.toml")
    main(["-s", str(settings), "run", "-o", out])
    main(["-s", str(settings), "run", "-o", out, "--webui", "--", "--verbose"])
    assert calls[0][:2] == ["--config", out]
    assert "--no-webui" in calls[0] and "--webui" not in calls[0]
    assert "--webui" in calls[1] and calls[1][-1] == "--verbose"


def test_check(settings: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["-s", str(settings), "check", "Beelink SER5 16GB", "--price", "150"])
    assert "[nuc] PASS" in capsys.readouterr().out
    main(["-s", str(settings), "check", "Intel NUC for parts", "--price", "10"])
    out = capsys.readouterr().out
    assert "antikeyword" in out and "price outside" in out
    main(["-s", str(settings), "check", "String trimmer", "info: https://x.example/?q=qNuc"])
    assert "[nuc] DROP: no required keyword" in capsys.readouterr().out


def test_run_refuses_without_env(
    settings: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert main(["-s", str(settings), "run"]) == 2
    assert "ANTHROPIC_API_KEY, NTFY_TOPIC" in capsys.readouterr().err


def test_bad_settings_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-s", str(tmp_path / "missing.toml"), "render"]) == 2
    assert "deal-hunter init" in capsys.readouterr().err


def test_run_ignores_packages_in_cwd(settings: Path, tmp_path: Path) -> None:
    # A planted ai_marketplace_monitor in the working directory must not load:
    # it would run with the API keys in the environment.
    import os
    import subprocess
    import sys

    fake = tmp_path / "ai_marketplace_monitor"
    fake.mkdir()
    (fake / "__init__.py").write_text("raise SystemExit('HIJACKED')\n")
    env = {**os.environ, "ANTHROPIC_API_KEY": "k", "NTFY_TOPIC": "t"}
    exe = Path(sys.executable).parent / "deal-hunter"
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [str(exe), "-s", str(settings), "run", "-o", "c.toml", "--", "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "HIJACKED" not in result.stdout + result.stderr
    assert "AI Marketplace Monitor, version" in result.stdout


@pytest.fixture
def aimm_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], object]]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("NTFY_TOPIC", "t")
    calls: list[tuple[list[str], object]] = []
    monkeypatch.setattr(
        "deal_hunter.launch.main", lambda argv, session, sandbox: calls.append((argv, session))
    )
    return calls


def write_settings(path: Path, facebook: str) -> Path:
    path.write_text(SETTINGS + "[facebook]\n" + facebook)
    return path


def test_run_passes_the_session(tmp_path: Path, aimm_calls: list[tuple[list[str], object]]) -> None:
    from deal_hunter.session import Session

    fb = tmp_path / "fb.json"
    s = write_settings(tmp_path / "s.toml", f'session_file = "{fb}"\n')
    assert main(["-s", str(s), "run", "-o", str(tmp_path / "c.toml")]) == 0
    argv, session = aimm_calls[0]
    assert session == Session(fb, required=False)
    assert "--headless" not in argv  # a person may need to log in


def test_run_anonymous_has_no_session(
    tmp_path: Path, aimm_calls: list[tuple[list[str], object]]
) -> None:
    s = write_settings(tmp_path / "s.toml", "anonymous = true\n")
    main(["-s", str(s), "run", "-o", str(tmp_path / "c.toml")])
    argv, session = aimm_calls[0]
    assert session is None and "--headless" in argv


def test_run_required_session_is_headless(
    tmp_path: Path, aimm_calls: list[tuple[list[str], object]]
) -> None:
    from fakes import LOGIN_COOKIE, FakeContext

    from deal_hunter.session import Session, save

    fb = tmp_path / "fb.json"
    save(FakeContext([LOGIN_COOKIE]), fb)
    s = write_settings(tmp_path / "s.toml", f'session_file = "{fb}"\nrequire_session = true\n')
    assert main(["-s", str(s), "run", "-o", str(tmp_path / "c.toml")]) == 0
    argv, session = aimm_calls[0]
    assert session == Session(fb, required=True) and "--headless" in argv


def test_run_bad_session_fails_before_starting_even_if_optional(
    tmp_path: Path,
    aimm_calls: list[tuple[list[str], object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fb = tmp_path / "fb.json"
    fb.write_text("junk")
    s = write_settings(tmp_path / "s.toml", f'session_file = "{fb}"\n')
    assert main(["-s", str(s), "run", "-o", str(tmp_path / "c.toml")]) == 2
    assert "not a saved session" in capsys.readouterr().err
    assert aimm_calls == []


@pytest.mark.parametrize(("content", "match"), [(None, "doesn't exist"), ("junk", "not a saved")])
def test_run_required_session_fails_before_starting(
    tmp_path: Path,
    aimm_calls: list[tuple[list[str], object]],
    capsys: pytest.CaptureFixture[str],
    content: str | None,
    match: str,
) -> None:
    fb = tmp_path / "fb.json"
    if content is not None:
        fb.write_text(content)
    s = write_settings(tmp_path / "s.toml", f'session_file = "{fb}"\nrequire_session = true\n')
    assert main(["-s", str(s), "run", "-o", str(tmp_path / "c.toml")]) == 2
    err = capsys.readouterr().err
    assert match in err and "deal-hunter login" in err
    assert aimm_calls == []


def test_login_saves_to_the_settings_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fb = tmp_path / "fb.json"
    s = write_settings(tmp_path / "s.toml", f'session_file = "{fb}"\n')
    paths: list[Path] = []
    monkeypatch.setattr("deal_hunter.cli.interactive_login", paths.append)
    monkeypatch.setattr("deal_hunter.cli.exclude_from_backups", lambda p: paths.append(p) or False)
    assert main(["-s", str(s), "login"]) == 0
    assert paths == [fb, fb]
    out, err = capsys.readouterr()
    assert "don't commit or share it" in out
    assert "couldn't exclude" in err


def test_login_errors_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from deal_hunter.session import SessionError

    def fail(path: Path) -> None:
        raise SessionError("not logged in after 300 seconds; nothing saved.")

    monkeypatch.setattr("deal_hunter.cli.interactive_login", fail)
    s = write_settings(tmp_path / "s.toml", "")
    assert main(["-s", str(s), "login"]) == 2
    assert "nothing saved" in capsys.readouterr().err


def test_login_refuses_anonymous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deal_hunter.cli.interactive_login", pytest.fail)
    s = write_settings(tmp_path / "s.toml", "anonymous = true\n")
    assert main(["-s", str(s), "login"]) == 2


def test_web_warns_off_loopback(
    settings: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, int, list[str]]] = []
    monkeypatch.setattr(
        "deal_hunter.dashboard.serve",
        lambda s, home, host, port, extra: calls.append((host, port, extra)),
    )
    assert main(["-s", str(settings), "web", "--host", "192.0.2.1", "--allow-host", "nuc.lan"]) == 0
    assert calls == [("192.0.2.1", 8740, ["nuc.lan"])]
    assert "no login" in capsys.readouterr().err
    assert main(["-s", str(settings), "web"]) == 0
    assert "no login" not in capsys.readouterr().err


def test_web_port_in_use(
    settings: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def busy(*args: object) -> None:
        raise OSError(48, "Address already in use")

    monkeypatch.setattr("deal_hunter.dashboard.serve", busy)
    assert main(["-s", str(settings), "web"]) == 1
    assert "Try --port" in capsys.readouterr().err


def test_run_copies_a_new_seed_once(
    tmp_path: Path, aimm_calls: list[tuple[list[str], object]]
) -> None:
    from fakes import LOGIN_COOKIE, FakeContext

    from deal_hunter.session import load, save

    seed = tmp_path / "seed" / "fb.json"
    seed.parent.mkdir()
    save(FakeContext([LOGIN_COOKIE]), seed)
    fb = tmp_path / "state" / "fb.json"
    s = write_settings(tmp_path / "s.toml", f'session_file = "{fb}"\nrequire_session = true\n')
    argv = ["-s", str(s), "run", "-o", str(tmp_path / "c.toml"), "--session-seed", str(seed)]
    assert main(argv) == 0
    assert load(fb) == load(seed)
    fb.write_text('{"cookies": [], "origins": []}')  # the monitor refreshed its copy
    assert main(argv) == 0
    assert load(fb) == {"cookies": [], "origins": []}  # the same seed isn't copied back
    assert len(aimm_calls) == 2


@pytest.mark.parametrize("flag", [["--session-seed", "x.json"], ["--wait-for-session", "60"]])
def test_seed_and_wait_need_require_session(
    tmp_path: Path,
    aimm_calls: list[tuple[list[str], object]],
    capsys: pytest.CaptureFixture[str],
    flag: list[str],
) -> None:
    s = write_settings(tmp_path / "s.toml", f'session_file = "{tmp_path / "fb.json"}"\n')
    assert main(["-s", str(s), "run", "-o", str(tmp_path / "c.toml"), *flag]) == 2
    assert "require_session" in capsys.readouterr().err
    assert aimm_calls == []


def test_run_waits_for_a_session_then_starts_over(
    tmp_path: Path,
    aimm_calls: list[tuple[list[str], object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deal_hunter import session
    from deal_hunter.events import EVENTS_NAME, read_events

    sleeps: list[float] = []
    execs: list[list[str]] = []
    monkeypatch.setattr("deal_hunter.cli.time.sleep", sleeps.append)
    monkeypatch.setattr("deal_hunter.cli.os.execv", lambda exe, argv: execs.append(argv))
    fb = tmp_path / "fb.json"
    s = write_settings(tmp_path / "s.toml", f'session_file = "{fb}"\nrequire_session = true\n')
    argv = ["-s", str(s), "run", "-o", str(tmp_path / "c.toml"), "--wait-for-session", "900"]
    assert main(argv) == 0
    assert sleeps == [900] and len(execs) == 1 and aimm_calls == []
    (event,) = read_events(session.AIMM_HOME / EVENTS_NAME)
    assert event["kind"] == "error" and "Waiting for a Facebook session" in event["message"]


def test_run_waits_for_a_new_seed_without_loading_facebook(
    tmp_path: Path,
    aimm_calls: list[tuple[list[str], object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fakes import LOGIN_COOKIE, FakeContext

    from deal_hunter.session import copy_seed, save

    seed = tmp_path / "seed.json"
    save(FakeContext([LOGIN_COOKIE]), seed)
    fb = tmp_path / "state" / "fb.json"
    copy_seed(seed, fb)  # already copied before the session failed
    sleeps: list[float] = []
    execs: list[list[str]] = []
    monkeypatch.setattr("deal_hunter.cli.time.sleep", sleeps.append)
    monkeypatch.setattr("deal_hunter.cli.os.execv", lambda exe, argv: execs.append(argv))
    monkeypatch.setenv("DEAL_HUNTER_AWAITING_SEED", "1")
    s = write_settings(tmp_path / "s.toml", f'session_file = "{fb}"\nrequire_session = true\n')
    argv = ["-s", str(s), "run", "-o", str(tmp_path / "c.toml")]
    argv += ["--session-seed", str(seed), "--wait-for-session", "900"]
    assert main(argv) == 0
    assert aimm_calls == [] and sleeps == [900] and len(execs) == 1
    assert os.environ["DEAL_HUNTER_AWAITING_SEED"] == "1"

    seed.write_text('{"cookies": [{"name": "c_user", "value": "2", "domain": ".facebook.com"}]}')
    assert main(argv) == 0  # a new seed: copied and started
    assert len(aimm_calls) == 1


@pytest.mark.parametrize(("restarts", "delay"), [(None, 600), ("2", 2400), ("20", 6 * 3600)])
def test_run_restarts_after_other_errors_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restarts: str | None,
    delay: int,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("NTFY_TOPIC", "t")

    def broken(argv: list[str], session: object, sandbox: bool) -> None:
        raise RuntimeError("Failed to navigate to https://www.facebook.com/x\nmore")

    sleeps: list[float] = []
    monkeypatch.setattr("deal_hunter.launch.main", broken)
    monkeypatch.setattr("deal_hunter.cli.time.sleep", sleeps.append)
    monkeypatch.setattr("deal_hunter.cli.os.execv", lambda exe, argv: None)
    if restarts is not None:
        monkeypatch.setenv("DEAL_HUNTER_RESTARTS", restarts)
    s = write_settings(tmp_path / "s.toml", f'session_file = "{tmp_path / "fb.json"}"\n')
    s.write_text(SETTINGS + "[facebook]\nanonymous = true\n")
    argv = ["-s", str(s), "run", "-o", str(tmp_path / "c.toml"), "--restart-after", "600"]
    assert main(argv) == 0
    assert sleeps == [delay]
    assert os.environ["DEAL_HUNTER_RESTARTS"] == str(int(restarts or 0) + 1)


def test_run_without_restart_after_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("NTFY_TOPIC", "t")
    monkeypatch.setattr("deal_hunter.launch.main", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    s = tmp_path / "s.toml"
    s.write_text(SETTINGS + "[facebook]\nanonymous = true\n")
    with pytest.raises(RuntimeError):
        main(["-s", str(s), "run", "-o", str(tmp_path / "c.toml")])


def test_interrupt_exits_130(settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deal_hunter import cli

    def interrupted(*a: object, **k: object) -> int:
        cli._interrupt(15, None)  # what SIGTERM does
        return 0

    monkeypatch.setattr(cli, "_check", interrupted)
    assert main(["-s", str(settings), "check", "nuc"]) == 130


def test_old_settings_name_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nuc-hunter.toml").write_text(SETTINGS)
    assert main(["render", "-o", str(tmp_path / "c.toml")]) == 0
    assert "rename it to deal-hunter.toml" in capsys.readouterr().err


def test_unattended_run_deletes_a_planted_aimm_config(
    tmp_path: Path, aimm_calls: list[tuple[list[str], object]]
) -> None:
    from fakes import LOGIN_COOKIE, FakeContext

    from deal_hunter import session
    from deal_hunter.session import save

    planted = session.AIMM_HOME / "config.toml"
    planted.symlink_to(tmp_path / "elsewhere.toml")  # dangling counts too
    fb = tmp_path / "fb.json"
    save(FakeContext([LOGIN_COOKIE]), fb)
    s = write_settings(tmp_path / "s.toml", f'session_file = "{fb}"\nrequire_session = true\n')
    assert main(["-s", str(s), "run", "-o", str(tmp_path / "c.toml")]) == 0
    assert not os.path.lexists(planted) and aimm_calls


def test_attended_run_only_warns_about_aimm_config(
    settings: Path,
    aimm_calls: list[tuple[list[str], object]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from deal_hunter import session

    mine = session.AIMM_HOME / "config.toml"
    mine.write_text("# the user's own\n")
    main(["-s", str(settings), "run", "-o", str(tmp_path / "c.toml")])
    assert mine.exists() and "will be merged" in capsys.readouterr().err
