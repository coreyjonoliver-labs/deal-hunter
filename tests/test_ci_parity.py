"""CI's checks, runnable before the push (spec ci-parity).

The pre-push hook (`scripts/git-hooks/pre-push`) is run against throwaway git
repos: with a stub `gitleaks` for argv, version and missing-binary handling,
and with the real one (skipped when it isn't installed) for what it actually
catches. The workflows are read as text, one line at a time.

No literal secret-looking value is committed here: CI's gitleaks scans every
commit of this repo with no allowlist, so the planted key is built at runtime.
"""

from __future__ import annotations

import secrets
import shutil
import stat
import string
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "scripts" / "git-hooks" / "pre-push"
SECURITY_YML = ROOT / ".github" / "workflows" / "security.yml"
BUILD_YML = ROOT / ".github" / "workflows" / "build.yml"
ZEROS = "0" * 40
SYSTEM_DIRS = "/usr/bin:/bin"

STUB = """#!/bin/sh
if [ "$1" = version ]; then
    echo "$STUB_VERSION"
    exit 0
fi
printf '%s\\n' "$@" >"$STUB_LOG"
prev=""
for arg in "$@"; do
    case "$prev" in
        --config) cp "$arg" "$STUB_LOG.config" ;;
        --gitleaks-ignore-path)
            printf '%s\\n' "$arg" >"$STUB_LOG.dir"
            wc -c <"$arg/.gitleaksignore" >"$STUB_LOG.ignore"
            ;;
    esac
    prev="$arg"
done
printf '%s\\n' "${GIT_ATTR_SOURCE-unset}" >"$STUB_LOG.attr"
printf '%b\\n' "$STUB_OUT" >&2
exit "${STUB_EXIT:-0}"
"""

CLEAN_OUT = "8:00PM INF 1 commits scanned.\\n8:00PM INF no leaks found"
ERR_LINE = '8:00PM ERR error="stderr is not empty"'
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def run(
    argv: list[str], cwd: Path, env: dict[str, str], stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv built by the tests
        argv, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False
    )


def git_env(home: Path, path: str) -> dict[str, str]:
    """An environment that ignores the developer's git config (signing included)."""
    env = {
        "PATH": path,
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    env["TMPDIR"] = tempfile.gettempdir()  # where the hook's mktemp writes
    return env


class Repo:
    def __init__(self, path: Path, env: dict[str, str]) -> None:
        self.path = path
        self.env = env
        self.base_path = env["PATH"]
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        done = run(["git", "-c", "commit.gpgsign=false", *args], self.path, self.env)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def commit(self, name: str, text: str) -> str:
        (self.path / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", f"add {name}")
        return self.git("rev-parse", "HEAD")

    def push_hook(
        self, stdin: str, path: str, remote: str = "origin"
    ) -> subprocess.CompletedProcess[str]:
        return run([str(HOOK), remote], self.path, {**self.env, "PATH": path}, stdin)


def push_line(branch: str, sha: str) -> str:
    return f"refs/heads/{branch} {sha} refs/heads/{branch} {ZEROS}\n"


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    work = tmp_path / "work"
    work.mkdir()
    # git comes from a directory of its own, so the hook sees no other tool
    # the developer has installed (gitleaks, say). The macOS /usr/bin/git shim
    # isn't used when a real git is on PATH, as it writes a cache outside the
    # tmp dir.
    tools = tmp_path / "tools"
    tools.mkdir()
    git = shutil.which("git")
    assert git is not None
    (tools / "git").symlink_to(git)
    return Repo(work, git_env(tmp_path / "home", f"{tools}:{SYSTEM_DIRS}"))


@pytest.fixture
def stub(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gitleaks"
    script.write_text(STUB)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def stub_run(
    repo: Repo,
    stub: Path,
    stdin: str,
    *,
    version: str = "8.30.1",
    exit_code: int = 0,
    out: str = CLEAN_OUT,
    remote: str = "origin",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    log = stub.parent / "stub.log"
    env = {
        **repo.env,
        "PATH": f"{stub}:{repo.base_path}",
        "STUB_VERSION": version,
        "STUB_LOG": str(log),
        "STUB_EXIT": str(exit_code),
        "STUB_OUT": out,
    }
    return run([str(HOOK), remote], repo.path, env, stdin), log


def hook_version() -> str:
    found = [
        line.split('"')[1]
        for line in HOOK.read_text().splitlines()
        if line.startswith('GITLEAKS_VERSION="')
    ]
    assert len(found) == 1
    return found[0]


def test_hook_is_an_executable_posix_sh_script_with_set_eu() -> None:
    lines = HOOK.read_text().splitlines()
    assert lines[0] == "#!/bin/sh"
    assert "set -eu" in lines
    assert HOOK.stat().st_mode & stat.S_IXUSR
    assert not any("eval " in line for line in lines if not line.lstrip().startswith("#"))


def test_hook_runs_gitleaks_with_ci_settings_over_unpushed_commits(repo: Repo, stub: Path) -> None:
    sha = repo.commit("a.txt", "hello\n")
    done, log = stub_run(repo, stub, push_line("feat", sha))
    assert done.returncode == 0, done.stderr
    argv = log.read_text().splitlines()
    assert argv[:2] == ["git", "."]
    for flag in ("--redact", "--no-banner", "--no-color", "--ignore-gitleaks-allow"):
        assert argv.count(flag) == 1
    # Plain text for any attributes, only this push's commits, scoped to the remote.
    assert argv[argv.index("--log-opts") + 1] == (
        f"--no-color --text --no-textconv --no-ext-diff --root --diff-merges=first-parent "
        f"{sha} --not --remotes=origin"
    )
    assert Path(f"{log}.attr").read_text().strip() == EMPTY_TREE
    # CI's config, written outside the checkout; the ignore file is empty.
    assert Path(f"{log}.config").read_text() == "[extend]\nuseDefault = true\n"
    assert Path(f"{log}.ignore").read_text().strip() == "0"
    config_dir = Path(Path(f"{log}.dir").read_text().strip())
    assert argv[argv.index("--config") + 1] == str(config_dir / "gitleaks.toml")
    assert repo.path not in config_dir.parents
    assert not config_dir.exists()  # removed on exit


def test_hook_scans_every_pushed_tip_and_skips_deleted_branches(repo: Repo, stub: Path) -> None:
    first = repo.commit("a.txt", "one\n")
    second = repo.commit("b.txt", "two\n")
    stdin = push_line("gone", ZEROS) + push_line("one", first) + push_line("two", second)
    done, log = stub_run(repo, stub, stdin)
    assert done.returncode == 0, done.stderr
    argv = log.read_text().splitlines()
    opts = argv[argv.index("--log-opts") + 1]
    assert opts.endswith(f"{first} {second} --not --remotes=origin")


@pytest.mark.parametrize("remote", ["https://example.invalid/r.git", "a b", "x*", "a:b", ""])
def test_hook_excludes_nothing_when_the_remote_is_not_a_plain_name(
    repo: Repo, stub: Path, remote: str
) -> None:
    sha = repo.commit("a.txt", "hello\n")
    done, log = stub_run(repo, stub, push_line("feat", sha), remote=remote)
    assert done.returncode == 0, done.stderr
    opts = Path(log).read_text().splitlines()
    assert opts[opts.index("--log-opts") + 1].endswith(f"{sha} ")  # no --not at all


def test_hook_exits_zero_without_scanning_when_the_remote_already_has_everything(
    repo: Repo, stub: Path
) -> None:
    sha = repo.commit("a.txt", "hello\n")
    repo.git("update-ref", "refs/remotes/origin/main", sha)
    done, log = stub_run(repo, stub, push_line("main", sha), version="0.0.0")
    assert done.returncode == 0, done.stderr
    assert not log.exists()


def test_hook_scans_what_only_another_remote_has(repo: Repo, stub: Path) -> None:
    sha = repo.commit("a.txt", "hello\n")
    repo.git("update-ref", "refs/remotes/other/main", sha)
    done, log = stub_run(repo, stub, push_line("main", sha))
    assert done.returncode == 0, done.stderr
    assert log.exists()


def test_hook_exits_zero_without_scanning_when_only_deleting(repo: Repo, stub: Path) -> None:
    repo.commit("a.txt", "one\n")
    done, log = stub_run(repo, stub, push_line("gone", ZEROS), version="0.0.0")
    assert done.returncode == 0, done.stderr
    assert not log.exists()


def test_hook_exits_zero_with_no_ref_lines_even_without_gitleaks(repo: Repo) -> None:
    assert repo.push_hook("", repo.base_path).returncode == 0


def test_hook_blocks_when_gitleaks_reports_a_finding(repo: Repo, stub: Path) -> None:
    sha = repo.commit("a.txt", "one\n")
    done, _ = stub_run(repo, stub, push_line("feat", sha), exit_code=1)
    assert done.returncode != 0
    assert "blocked" in done.stderr


@pytest.mark.parametrize(
    "out",
    [
        ERR_LINE + "\n8:00PM INF 0 commits scanned.\n8:00PM INF no leaks found",
        ERR_LINE + "\n" + CLEAN_OUT,
        "8:00PM INF 0 commits scanned.\n8:00PM INF no leaks found",
        "8:00PM INF no leaks found",
        "",
    ],
    ids=["error-and-zero", "error-only", "zero-commits", "no-count", "silent"],
)
def test_hook_blocks_when_gitleaks_exits_zero_but_did_not_scan(
    repo: Repo, stub: Path, out: str
) -> None:
    sha = repo.commit("a.txt", "one\n")
    done, _ = stub_run(repo, stub, push_line("feat", sha), out=out)
    assert done.returncode != 0
    assert "pre-push:" in done.stderr


def test_hook_scans_a_push_of_files_even_when_gitleaks_counts_one_commit(
    repo: Repo, stub: Path
) -> None:
    repo.commit("a.txt", "one\n")
    sha = repo.commit("b.txt", "two\n")
    done, _ = stub_run(repo, stub, push_line("feat", sha), out="8:00PM INF 1 commits scanned.")
    assert done.returncode == 0, done.stderr


def test_hook_exits_zero_without_scanning_a_push_of_only_empty_commits(
    repo: Repo, stub: Path
) -> None:
    repo.commit("a.txt", "one\n")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    repo.git("commit", "-q", "--allow-empty", "-m", "empty")
    sha = repo.git("rev-parse", "HEAD")
    done, log = stub_run(repo, stub, push_line("main", sha))
    assert done.returncode == 0, done.stderr
    assert not log.exists()


def test_hook_exits_zero_without_scanning_an_empty_ours_merge(repo: Repo, stub: Path) -> None:
    repo.commit("a.txt", "one\n")
    repo.git("checkout", "-q", "-b", "side")
    repo.commit("b.txt", "two\n")
    repo.git("checkout", "-q", "main")
    repo.commit("c.txt", "three\n")
    repo.git("update-ref", "refs/remotes/origin/main", "main")
    repo.git("update-ref", "refs/remotes/origin/side", "side")
    repo.git("merge", "-q", "--no-ff", "-s", "ours", "side", "-m", "merge")
    done, log = stub_run(repo, stub, push_line("main", repo.git("rev-parse", "HEAD")))
    assert done.returncode == 0, done.stderr
    assert not log.exists()


def test_hook_blocks_with_an_install_hint_when_gitleaks_is_missing(repo: Repo) -> None:
    sha = repo.commit("a.txt", "one\n")
    done = repo.push_hook(push_line("feat", sha), repo.base_path)
    assert done.returncode != 0
    assert f"Install gitleaks {hook_version()}" in done.stderr


def test_hook_blocks_with_an_install_hint_on_a_different_gitleaks_version(
    repo: Repo, stub: Path
) -> None:
    sha = repo.commit("a.txt", "one\n")
    done, log = stub_run(repo, stub, push_line("feat", sha), version="8.0.0")
    assert done.returncode != 0
    assert f"Install gitleaks {hook_version()}" in done.stderr
    assert "8.0.0" in done.stderr
    assert not log.exists()


def test_hook_blocks_an_object_name_that_is_not_hex(repo: Repo, stub: Path) -> None:
    done, log = stub_run(repo, stub, "refs/heads/x $(touch pwned) refs/heads/x abc\n")
    assert done.returncode != 0
    assert not log.exists()
    assert not (repo.path / "pwned").exists()


# The real gitleaks, when installed at the version CI uses.


def real_gitleaks_ok() -> bool:
    exe = shutil.which("gitleaks")
    if exe is None:
        return False
    done = subprocess.run(  # noqa: S603 — fixed argv
        [exe, "version"], capture_output=True, text=True, check=False
    )
    return done.stdout.strip() == hook_version()


needs_gitleaks = pytest.mark.skipif(
    not real_gitleaks_ok(), reason="gitleaks (the version CI uses) is not on PATH"
)


def real_path(repo: Repo) -> str:
    exe = shutil.which("gitleaks")
    assert exe is not None
    return f"{Path(exe).parent}:{repo.base_path}"


def planted_key_line(suffix: str = "") -> str:
    """A key gitleaks recognises, assembled at runtime (never a committed literal)."""
    prefix = "gh" + "p_"
    body = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
    return f'token = "{prefix}{body}"{suffix}\n'


@needs_gitleaks
def test_real_gitleaks_blocks_a_planted_key_and_redacts_it(repo: Repo) -> None:
    line = planted_key_line()
    sha = repo.commit("leak.txt", line)
    done = repo.push_hook(push_line("feat", sha), real_path(repo))
    assert done.returncode != 0
    output = done.stdout + done.stderr
    assert "leaks found: 1" in output
    assert line.split('"')[1] not in output


@needs_gitleaks
def test_real_gitleaks_ignores_a_gitleaks_allow_comment(repo: Repo) -> None:
    sha = repo.commit("leak.txt", planted_key_line("  # gitleaks:allow"))
    done = repo.push_hook(push_line("feat", sha), real_path(repo))
    assert done.returncode != 0


@needs_gitleaks
def test_real_gitleaks_ignores_a_repo_gitleaksignore_and_config(repo: Repo) -> None:
    line = planted_key_line()
    (repo.path / ".gitleaks.toml").write_text("[allowlist]\nregexes = ['.*']\n")
    repo.git("add", ".gitleaks.toml")
    sha = repo.commit("leak.txt", line)
    done = repo.push_hook(push_line("feat", sha), real_path(repo))
    assert done.returncode != 0


@needs_gitleaks
def test_real_gitleaks_passes_clean_commits_and_skips_those_already_on_a_remote(
    repo: Repo,
) -> None:
    leaked = repo.commit("leak.txt", planted_key_line())
    # The leak is already on a remote-tracking ref, so it isn't rescanned.
    repo.git("update-ref", "refs/remotes/origin/main", leaked)
    clean = repo.commit("ok.txt", "nothing secret here\n")
    done = repo.push_hook(push_line("feat", clean), real_path(repo))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 commits scanned" in done.stderr
    assert "no leaks found" in done.stderr


@needs_gitleaks
def test_real_gitleaks_passes_a_clean_merge_only_push(repo: Repo) -> None:
    repo.commit("a.txt", "one\n")
    repo.git("checkout", "-q", "-b", "side")
    repo.commit("b.txt", "two\n")
    repo.git("checkout", "-q", "main")
    repo.commit("c.txt", "three\n")
    repo.git("update-ref", "refs/remotes/origin/main", "main")
    repo.git("update-ref", "refs/remotes/origin/side", "side")
    repo.git("merge", "-q", "--no-ff", "side", "-m", "merge")
    done = repo.push_hook(push_line("main", repo.git("rev-parse", "HEAD")), real_path(repo))
    assert done.returncode == 0, done.stderr


# gitleaks 8.30.1 says "no leaks found" and exits 0 in each of these. The hook
# must still block the planted key.


def bypass_negative_attribute_pattern(repo: Repo) -> str:
    (repo.path / ".gitattributes").write_text("!x foo\n")
    return repo.commit("leak.txt", planted_key_line())


def bypass_diff_attribute(repo: Repo) -> str:
    (repo.path / ".gitattributes").write_text("leak.txt -diff\n")
    return repo.commit("leak.txt", planted_key_line())


def bypass_binary_attribute(repo: Repo) -> str:
    (repo.path / ".gitattributes").write_text("* binary\n")
    return repo.commit("leak.txt", planted_key_line())


def bypass_coloured_git_output(repo: Repo) -> str:
    repo.git("config", "color.ui", "always")
    return repo.commit("leak.txt", planted_key_line())


def bypass_ambiguous_ref(repo: Repo) -> str:
    sha = repo.commit("leak.txt", planted_key_line())
    repo.git("branch", sha)  # a branch named like the commit it points at
    return sha


def bypass_secret_only_in_a_merge_commit(repo: Repo) -> str:
    """A key added while resolving a merge: only the merge's own diff has it."""
    repo.commit("a.txt", "one\n")
    repo.git("checkout", "-q", "-b", "side")
    repo.commit("b.txt", "two\n")
    repo.git("checkout", "-q", "main")
    repo.commit("c.txt", "three\n")
    repo.git("merge", "-q", "--no-commit", "--no-ff", "side")
    (repo.path / "leak.txt").write_text(planted_key_line())
    repo.git("add", "leak.txt")
    repo.git("commit", "-q", "-m", "merge")
    return repo.git("rev-parse", "HEAD")


def bypass_replaced_commit(repo: Repo) -> str:
    clean = repo.commit("ok.txt", "nothing secret here\n")
    leaked = repo.commit("leak.txt", planted_key_line())
    repo.git("replace", leaked, clean)  # git log would show the clean commit
    return leaked


def bypass_hidden_root_commit(repo: Repo) -> str:
    repo.git("config", "log.showRoot", "false")
    return repo.commit("leak.txt", planted_key_line())


BYPASSES = [
    bypass_secret_only_in_a_merge_commit,
    bypass_replaced_commit,
    bypass_hidden_root_commit,
    bypass_negative_attribute_pattern,
    bypass_diff_attribute,
    bypass_binary_attribute,
    bypass_coloured_git_output,
    bypass_ambiguous_ref,
]


@needs_gitleaks
@pytest.mark.parametrize("plant", BYPASSES, ids=lambda f: f.__name__.removeprefix("bypass_"))
def test_real_gitleaks_still_blocks_a_planted_key_through_each_bypass(
    repo: Repo, plant: Callable[[Repo], str]
) -> None:
    sha = plant(repo)
    done = repo.push_hook(push_line("feat", sha), real_path(repo))
    assert done.returncode != 0, done.stderr
    assert "leaks found: 1" in done.stderr


# CI's gitleaks step (security.yml), run as the workflow runs it.


def ci_gitleaks_step() -> tuple[str, dict[str, str]]:
    """The `gitleaks` step's `run:` script and its `env:`, read a line at a time."""
    lines = SECURITY_YML.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line == "      - name: gitleaks"]
    assert len(starts) == 1
    end = starts[0] + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith("        ")):
        end += 1
    step = lines[starts[0] + 1 : end]
    env: dict[str, str] = {}
    script: list[str] = []
    mode = ""
    for line in step:
        if line == "        env:":
            mode = "env"
        elif line == "        run: |":
            mode = "run"
        elif mode == "env" and line.startswith("          "):
            key, _, value = line.strip().partition(": ")
            env[key] = value.strip('"')
        elif mode == "run":
            script.append(line[10:] if line.strip() else "")
    assert script, "no run: block"
    return "\n".join(script) + "\n", env


def run_ci_step(repo: Repo, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    runner_temp = tmp_path / "runner-temp"
    config = runner_temp / "gitleaks-config"
    config.mkdir(parents=True)
    exe = shutil.which("gitleaks")
    assert exe is not None
    (runner_temp / "gitleaks").symlink_to(exe)
    (config / "gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    (config / ".gitleaksignore").write_text("")
    script, env = ci_gitleaks_step()
    path = runner_temp / "step.sh"
    path.write_text(script)
    return run(
        ["sh", "-e", str(path)], repo.path, {**repo.env, **env, "RUNNER_TEMP": str(runner_temp)}
    )


def test_ci_gitleaks_step_reads_attributes_from_the_empty_tree() -> None:
    script, env = ci_gitleaks_step()
    assert env == {"GIT_ATTR_SOURCE": EMPTY_TREE, "GIT_NO_REPLACE_OBJECTS": "1"}
    for flag in (
        "--ignore-gitleaks-allow",
        "--redact",
        "--no-color",
        "--text",
        "--all",
        "--root",
        "--diff-merges=first-parent",
    ):
        assert flag in script


def test_hook_has_the_same_git_protections_as_the_ci_step() -> None:
    hook = HOOK.read_text()
    for needle in ("GIT_NO_REPLACE_OBJECTS=1", "--root", "--diff-merges=first-parent"):
        assert needle in hook, needle
    assert "--no-merges" not in hook


@needs_gitleaks
def test_ci_gitleaks_step_passes_a_clean_repo(repo: Repo, tmp_path: Path) -> None:
    repo.commit("ok.txt", "nothing secret here\n")
    done = run_ci_step(repo, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


@needs_gitleaks
@pytest.mark.parametrize(
    "plant",
    [lambda repo: repo.commit("leak.txt", planted_key_line()), *BYPASSES],
    ids=["plain", *(f.__name__.removeprefix("bypass_") for f in BYPASSES)],
)
def test_ci_gitleaks_step_fails_for_a_planted_key_through_each_bypass(
    repo: Repo, tmp_path: Path, plant: Callable[[Repo], str]
) -> None:
    plant(repo)
    done = run_ci_step(repo, tmp_path)
    assert done.returncode != 0
    assert "leaks found: 1" in done.stdout


@needs_gitleaks
def test_ci_gitleaks_step_finds_a_key_on_a_branch_that_is_not_checked_out(
    repo: Repo, tmp_path: Path
) -> None:
    repo.commit("ok.txt", "nothing secret here\n")
    repo.git("checkout", "-q", "-b", "other")
    repo.commit("leak.txt", planted_key_line())
    repo.git("checkout", "-q", "main")
    done = run_ci_step(repo, tmp_path)
    assert done.returncode != 0
    assert "leaks found: 1" in done.stdout


def test_ci_gitleaks_step_fails_when_gitleaks_scans_nothing(tmp_path: Path) -> None:
    # An empty repo has no commits to scan; exit 0 from gitleaks isn't enough.
    work = tmp_path / "work"
    work.mkdir()
    empty = Repo(work, git_env(tmp_path / "home", SYSTEM_DIRS))
    runner_temp = tmp_path / "runner-temp"
    (runner_temp / "gitleaks-config").mkdir(parents=True)
    stub_exe = runner_temp / "gitleaks"
    stub_exe.write_text('#!/bin/sh\necho "8:00PM INF 0 commits scanned." >&2\n')
    stub_exe.chmod(stub_exe.stat().st_mode | stat.S_IXUSR)
    script, env = ci_gitleaks_step()
    path = runner_temp / "step.sh"
    path.write_text(script)
    done = run(
        ["sh", "-e", str(path)], empty.path, {**empty.env, **env, "RUNNER_TEMP": str(runner_temp)}
    )
    assert done.returncode != 0
    assert "scanned no commits" in done.stdout


# The deliberate test failure when promtool is required but missing.


def run_promtool_test(tmp_path: Path, *, require: bool) -> subprocess.CompletedProcess[str]:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    env = {"PATH": str(empty), "HOME": str(tmp_path), "TMPDIR": tempfile.gettempdir()}
    if require:
        env["DEAL_HUNTER_REQUIRE_PROMTOOL"] = "1"
    return run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "tests/test_deploy.py::test_prometheus_rules_pass_their_unit_tests",
        ],
        ROOT,
        env,
    )


def test_missing_promtool_fails_when_required(tmp_path: Path) -> None:
    done = run_promtool_test(tmp_path, require=True)
    assert done.returncode != 0
    assert "promtool is not installed" in done.stdout
    assert "1 failed" in done.stdout


def test_missing_promtool_skips_when_not_required(tmp_path: Path) -> None:
    done = run_promtool_test(tmp_path, require=False)
    assert done.returncode == 0, done.stdout
    assert "1 skipped" in done.stdout


# The workflows, read a line at a time.


def env_values(path: Path, name: str) -> list[str]:
    prefix = f'{name}: "'
    return [
        line.strip()[len(prefix) :].rstrip('"')
        for line in path.read_text().splitlines()
        if line.strip().startswith(prefix)
    ]


def job_lines(path: Path, job: str) -> list[str]:
    """The lines of one top-level job under `jobs:` (a two-space-indented key)."""
    lines = path.read_text().splitlines()
    start = lines.index(f"  {job}:")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("   "):
            break
        body.append(line)
    return body


def test_hook_gitleaks_version_matches_security_workflow() -> None:
    assert env_values(SECURITY_YML, "GITLEAKS_VERSION") == [hook_version()]


def test_hook_uses_the_flags_of_the_ci_scan() -> None:
    script, _ = ci_gitleaks_step()
    scan = [line for line in script.splitlines() if line.strip().startswith("--")]
    ci_flags = {line.strip().split()[0] for line in scan} | {"--redact", "--no-banner"}
    hook = HOOK.read_text()
    for flag in sorted(ci_flags):
        assert flag in hook, flag


def test_build_workflow_test_job_installs_promtool_and_requires_it() -> None:
    body = job_lines(BUILD_YML, "test")
    text = "\n".join(body)
    assert text.count("sha256sum -c") == 1
    assert any(
        line.strip() == 'echo "${PROMETHEUS_SHA256}  prometheus.tar.gz" | sha256sum -c -'
        for line in body
    )
    assert len(env_values(BUILD_YML, "PROMETHEUS_VERSION")) == 1
    (digest,) = env_values(BUILD_YML, "PROMETHEUS_SHA256")
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    assert any("promtool" in line and "RUNNER_TEMP" in line for line in body)
    assert [line.strip() for line in body].count('DEAL_HUNTER_REQUIRE_PROMTOOL: "1"') == 1
    # The variable belongs to the pytest step, not some other one.
    pytest_at = [i for i, line in enumerate(body) if line.strip() == "- run: uv run pytest -q"]
    assert len(pytest_at) == 1
    assert body[pytest_at[0] + 1 : pytest_at[0] + 3] == [
        "        env:",
        '          DEAL_HUNTER_REQUIRE_PROMTOOL: "1"',
    ]
