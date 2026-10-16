"""CODEOWNERS must keep covering the load-bearing paths.

GitHub ignores a rule whose path matches nothing, so a rename would silently
drop ownership; every rule must name an existing path and at least one owner.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
OWNER_RE = re.compile(r"^@[A-Za-z0-9-]+(?:/[A-Za-z0-9_.-]+)?$")

# The paths the operator chose to own (spec graduate-established, req. 1).
REQUIRED = {
    "/.github/",
    "/.claude/",
    "/deploy/",
    "/Dockerfile",
    "/pyproject.toml",
    "/uv.lock",
    "/src/deal_hunter/launch.py",
    "/src/deal_hunter/discord.py",
    "/src/deal_hunter/dashboard.py",
    "/src/deal_hunter/dashboard.html",
}


def parse(text: str) -> list[tuple[str, list[str]]]:
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, *owners = line.split()
        rules.append((path, owners))
    return rules


def exists_exactly(root: Path, path: str) -> bool:
    """Whether `path` exists with exactly this case (GitHub matches case-sensitively,
    macOS's filesystem doesn't), and is a directory when it ends in `/`."""
    here = root
    for part in path.strip("/").split("/"):
        if not here.is_dir() or part not in os.listdir(here):
            return False
        here = here / part
    return here.is_dir() if path.endswith("/") else here.exists()


def problems(rules: list[tuple[str, list[str]]], root: Path) -> list[str]:
    found = []
    for path, owners in rules:
        if not path.startswith("/") or any(c in path for c in "*?["):
            found.append(f"{path}: use a literal path anchored at the repo root")
        elif not exists_exactly(root, path):
            found.append(f"{path}: no such path")
        if not owners:
            found.append(f"{path}: no owner")
        for owner in owners:
            if not OWNER_RE.match(owner):
                found.append(f"{path}: bad owner {owner!r}")
    return found


def test_every_rule_names_an_existing_path_and_an_owner() -> None:
    rules = parse(CODEOWNERS.read_text())
    assert rules, "CODEOWNERS has no rules"
    assert problems(rules, ROOT) == []


def test_the_chosen_paths_are_owned() -> None:
    owned = {path for path, _ in parse(CODEOWNERS.read_text())}
    assert sorted(REQUIRED - owned) == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("/src/deal_hunter/renamed.py @someone", "/src/deal_hunter/renamed.py: no such path"),
        ("/deploy/", "/deploy/: no owner"),
        ("/deploy/ someone", "/deploy/: bad owner 'someone'"),
        ("*.py @someone", "*.py: use a literal path anchored at the repo root"),
        ("/dockerfile @someone", "/dockerfile: no such path"),
        ("/Dockerfile/ @someone", "/Dockerfile/: no such path"),
    ],
)
def test_a_broken_rule_is_reported(line: str, expected: str) -> None:
    assert problems(parse(line), ROOT) == [expected]
