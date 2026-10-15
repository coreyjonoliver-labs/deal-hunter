"""The image references in deploy/ must agree, pin a digest, and not run ahead of the package.

The pinned version may lag pyproject.toml's: a version bump has to merge (and
`main` build it) before its image and digest exist, so the pin follows in a
later change. A pin newer than the package, a missing digest, or files that
disagree on the tag or digest all fail.

No YAML dependency: the files are read line by line, with regexes over the
known keys (the kustomizations' `images:` entries, the Compose and Deployment
`image:` lines).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "ghcr.io/coreyjonoliver-labs/deal-hunter"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

PYPROJECT = "pyproject.toml"
BASE = "deploy/kubernetes/base/kustomization.yaml"
EXAMPLE = "deploy/kubernetes/example/kustomization.yaml"
COMPOSE = "deploy/compose/compose.yaml"
DEPLOYMENT = "deploy/kubernetes/base/deployment.yaml"
PINNED = (BASE, EXAMPLE, COMPOSE)

_NEW_TAG_RE = re.compile(r"^\s*(?:-\s*)?newTag:\s*[\"']?([^\s\"'#]+)", re.MULTILINE)
_DIGEST_KEY_RE = re.compile(r"^\s*(?:-\s*)?digest:\s*[\"']?([^\s\"'#]*)", re.MULTILINE)
_ITEM_RE = re.compile(r"^\s*-\s")
_NAME_RE = re.compile(r"^\s*(?:-\s*)?name:\s*[\"']?([^\s\"'#]+)", re.MULTILINE)
_COMPOSE_IMAGE_RE = re.compile(
    rf"^\s*image:\s*{re.escape(IMAGE)}:([^\s@#]+)(?:@([^\s#]+))?", re.MULTILINE
)
_DEPLOYMENT_IMAGE_RE = re.compile(
    rf"^\s*image:\s*{re.escape(IMAGE)}:([^\s@#]+)(?:@([^\s#]+))?", re.MULTILINE
)
DEPLOYMENT_IMAGE_LINES = 3


def package_version(pyproject: str) -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert match, "no version in pyproject.toml"
    return match.group(1)


def image_entries(text: str) -> list[tuple[str | None, str | None]]:
    """The (newTag, digest) of each `images:` entry named exactly IMAGE.

    An entry runs from its `- ` line to the next one or the next line that
    isn't indented, whatever order its keys are in (kustomize accepts
    `- newTag: ...` before `name:`); None marks an absent key.
    """
    entries: list[tuple[str | None, str | None]] = []
    in_images = False
    block: list[str] | None = None
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if re.match(r"^images:\s*$", line):
            in_images, block = True, None
            continue
        if (
            in_images
            and line.strip()
            and not line[0].isspace()
            and not line.startswith(("#", "-"))  # "- name:" at column 0 is still a list item
        ):
            in_images, block = False, None
        if not in_images:
            continue
        if _ITEM_RE.match(line):
            block = []
            blocks.append(block)
        if block is not None:
            block.append(line)
    for lines in blocks:
        body = "\n".join(lines)
        name = _NAME_RE.search(body)
        if not name or name.group(1) != IMAGE:
            continue
        tag = _NEW_TAG_RE.search(body)
        digest = _DIGEST_KEY_RE.search(body)
        entries.append((tag.group(1) if tag else None, digest.group(1) if digest else None))
    return entries


def _version_key(text: str | None) -> tuple[int, ...] | None:
    if text is None or not re.fullmatch(r"\d+(?:\.\d+)*", text):
        return None
    return tuple(int(part) for part in text.split("."))


def _check_ref(path: str, tag: str | None, digest: str | None, version: str) -> list[str]:
    problems: list[str] = []
    pinned, package = _version_key(tag), _version_key(version)
    if pinned is None:
        problems.append(f"{path}: image tag {tag!r} isn't a release version")
    elif package is not None and pinned > package:
        problems.append(f"{path}: image tag {tag!r} is newer than the package version {version!r}")
    if digest is None or not DIGEST_RE.fullmatch(digest):
        problems.append(f"{path}: missing or malformed sha256 digest ({digest!r})")
    return problems


def check_pins(files: dict[str, str], version: str) -> list[str]:
    """Problems with the image pins in `files` (path -> text); empty when consistent."""
    problems: list[str] = []
    if _version_key(version) is None:
        # Otherwise the "newer than the package" guard would silently switch off.
        problems.append(f"{PYPROJECT}: version {version!r} isn't a plain release version")
    refs: list[tuple[str, str | None, str | None]] = []  # (path, tag, digest) per reference

    for path in (BASE, EXAMPLE):
        entries = image_entries(files[path])
        if len(entries) > 1:
            problems.append(f"{path}: {len(entries)} images entries named {IMAGE}, expected one")
        elif not entries:
            if path == BASE:
                problems.append(f"{path}: no images entry named {IMAGE}")
            elif re.search(r"^images:", files[path], re.MULTILINE):
                # A form this parser doesn't follow (flow style, ...) could
                # still carry a tag-only entry that kustomize applies.
                problems.append(
                    f"{path}: has an images: key but no entry for {IMAGE} that could be "
                    "checked; remove it to inherit the base's pin"
                )
            continue  # the overlay may inherit the base's pin
        else:
            tag, digest = entries[0]
            problems += _check_ref(path, tag, digest, version)
            refs.append((path, tag, digest))

    compose = _COMPOSE_IMAGE_RE.search(files[COMPOSE])
    if not compose:
        problems.append(f"{COMPOSE}: image line for {IMAGE} not found")
    else:
        problems += _check_ref(COMPOSE, compose.group(1), compose.group(2), version)
        refs.append((COMPOSE, compose.group(1), compose.group(2)))

    lines = _DEPLOYMENT_IMAGE_RE.findall(files[DEPLOYMENT])
    if len(lines) != DEPLOYMENT_IMAGE_LINES:
        problems.append(
            f"{DEPLOYMENT}: {len(lines)} image lines for {IMAGE}, expected {DEPLOYMENT_IMAGE_LINES}"
        )
    for tag, digest in lines:
        problems += _check_ref(DEPLOYMENT, tag, digest or None, version)
        refs.append((DEPLOYMENT, tag, digest or None))

    tags = [(path, t) for path, t, _ in refs if _version_key(t) is not None]
    if tags:
        reference_path, reference = tags[0]  # the base kustomization, when it has a pin
        for path, tag in tags:
            if tag != reference:
                problems.append(
                    f"{path}: image tag {tag!r} differs from {reference_path}'s {reference!r}"
                )
    valid = [(path, d) for path, _, d in refs if d and DIGEST_RE.fullmatch(d)]
    if len({d for _, d in valid}) > 1:
        problems.append(f"digests differ across files: {valid}")
    return problems


def read_real() -> tuple[dict[str, str], str]:
    files = {path: (ROOT / path).read_text() for path in (*PINNED, DEPLOYMENT)}
    return files, package_version((ROOT / PYPROJECT).read_text())


GOOD_DIGEST = "sha256:" + "ab" * 32
GOOD = {
    BASE: f"images:\n  - name: {IMAGE}\n    newTag: 1.2.3\n    digest: {GOOD_DIGEST}\n",
    EXAMPLE: f'images:\n  - name: {IMAGE}\n    newTag: "1.2.3"\n    digest: {GOOD_DIGEST}\n',
    COMPOSE: f"x-hardening: &h\n  image: {IMAGE}:1.2.3@{GOOD_DIGEST}   # moves\n",
    DEPLOYMENT: "".join(f"          image: {IMAGE}:1.2.3@{GOOD_DIGEST}\n" for _ in range(3)),
}


def test_committed_manifests_pin_the_package_version_and_one_digest() -> None:
    files, version = read_real()
    assert check_pins(files, version) == []


def test_checker_accepts_consistent_pins() -> None:
    assert check_pins(GOOD, "1.2.3") == []


def test_version_bump_can_merge_before_its_image_is_pinned() -> None:
    # The bump's image only exists once main builds it; pins follow later.
    assert check_pins(GOOD, "1.3.0") == []


def test_pins_newer_than_the_package_name_each_file() -> None:
    problems = check_pins(GOOD, "1.2.0")
    for path in (*PINNED, DEPLOYMENT):
        assert any(p.startswith(path) and "newer" in p for p in problems), path


def test_non_release_tag_fails() -> None:
    files = {**GOOD, COMPOSE: GOOD[COMPOSE].replace(":1.2.3@", ":latest@")}
    _only(check_pins(files, "1.2.3"), COMPOSE)


def test_one_stale_file_is_named_alone() -> None:
    files = {**GOOD, COMPOSE: GOOD[COMPOSE].replace("1.2.3", "1.2.2")}
    problems = check_pins(files, "1.2.3")
    assert len(problems) == 1
    assert problems[0].startswith(COMPOSE)


def test_missing_digest_fails_and_names_the_file() -> None:
    files = {**GOOD, BASE: f"images:\n  - name: {IMAGE}\n    newTag: 1.2.3\n"}
    problems = check_pins(files, "1.2.3")
    assert [p.split(":")[0] for p in problems] == [BASE]


def test_commented_out_digest_does_not_count() -> None:
    files = {**GOOD, EXAMPLE: GOOD[EXAMPLE].replace("    digest:", "    # digest:")}
    assert any(p.startswith(EXAMPLE) for p in check_pins(files, "1.2.3"))


def test_compose_without_digest_fails() -> None:
    files = {**GOOD, COMPOSE: f"  image: {IMAGE}:1.2.3\n"}
    assert any(p.startswith(COMPOSE) for p in check_pins(files, "1.2.3"))


def test_short_or_non_hex_digest_fails() -> None:
    for bad in ("sha256:abc123", "sha256:" + "zz" * 32, "sha256:" + "ab" * 33, "ab" * 32):
        files = {**GOOD, BASE: GOOD[BASE].replace(GOOD_DIGEST, bad)}
        problems = check_pins(files, "1.2.3")
        assert [p.split(":")[0] for p in problems] == [BASE], bad


def test_differing_digests_fail() -> None:
    other = "sha256:" + "cd" * 32
    files = {**GOOD, EXAMPLE: GOOD[EXAMPLE].replace(GOOD_DIGEST, other)}
    assert any("digests differ" in p for p in check_pins(files, "1.2.3"))


def _only(problems: list[str], path: str) -> None:
    assert problems and all(p.startswith(path) for p in problems), problems


def test_unrelated_image_entry_above_ours_is_ignored() -> None:
    other = "sha256:" + "cd" * 32
    extra = f"images:\n  - name: example.com/other\n    newTag: 9.9.9\n    digest: {other}\n"
    files = {
        **GOOD,
        BASE: GOOD[BASE].replace("images:\n", extra, 1),
    }
    assert check_pins(files, "1.2.3") == []


def test_unrelated_entry_holding_the_pin_does_not_satisfy_the_check() -> None:
    files = {**GOOD, BASE: GOOD[BASE].replace(f"name: {IMAGE}", "name: example.com/other")}
    _only(check_pins(files, "1.2.3"), BASE)


def test_mismatched_name_fails_and_names_the_file() -> None:
    files = {**GOOD, BASE: GOOD[BASE].replace(f"name: {IMAGE}", f"name: {IMAGE}x")}
    _only(check_pins(files, "1.2.3"), BASE)


def test_duplicate_entries_for_the_image_fail() -> None:
    entry = GOOD[BASE].split("images:\n")[1]
    files = {**GOOD, BASE: GOOD[BASE] + entry}
    assert any("2 images entries" in p for p in check_pins(files, "1.2.3"))


def test_overlay_without_images_entry_inherits_the_base_pin() -> None:
    files = {**GOOD, EXAMPLE: "resources:\n  - ../base\n"}
    assert check_pins(files, "1.2.3") == []


def test_overlay_tag_only_entry_fails() -> None:
    files = {**GOOD, EXAMPLE: f'images:\n  - name: {IMAGE}\n    newTag: "1.2.3"\n'}
    _only(check_pins(files, "1.2.3"), EXAMPLE)


def test_keys_after_the_images_block_are_not_read() -> None:
    tail = f"patches:\n  - patch: x\n    digest: {GOOD_DIGEST}\n    newTag: 1.2.3\n"
    files = {**GOOD, BASE: f"images:\n  - name: {IMAGE}\n{tail}"}
    _only(check_pins(files, "1.2.3"), BASE)


def test_compose_image_line_not_found_is_reported_as_such() -> None:
    files = {**GOOD, COMPOSE: f'  image: "{IMAGE}:1.2.3@{GOOD_DIGEST}"\n'}
    problems = check_pins(files, "1.2.3")
    assert problems == [f"{COMPOSE}: image line for {IMAGE} not found"]


def test_deployment_with_no_image_lines_fails() -> None:
    files = {**GOOD, DEPLOYMENT: "kind: Deployment\n"}
    _only(check_pins(files, "1.2.3"), DEPLOYMENT)


def test_deployment_with_wrong_line_count_fails() -> None:
    files = {**GOOD, DEPLOYMENT: GOOD[DEPLOYMENT].splitlines(keepends=True)[0]}
    _only(check_pins(files, "1.2.3"), DEPLOYMENT)


def test_deployment_line_without_digest_fails() -> None:
    files = {**GOOD, DEPLOYMENT: GOOD[DEPLOYMENT].replace(f"@{GOOD_DIGEST}", "", 1)}
    _only(check_pins(files, "1.2.3"), DEPLOYMENT)


def test_deployment_digest_differing_from_the_others_fails() -> None:
    other = "sha256:" + "cd" * 32
    files = {**GOOD, DEPLOYMENT: GOOD[DEPLOYMENT].replace(GOOD_DIGEST, other, 1)}
    assert any("digests differ" in p for p in check_pins(files, "1.2.3"))


def test_overlay_entry_with_keys_in_another_order_is_still_checked() -> None:
    # kustomize applies this entry and drops the base's digest.
    files = {**GOOD, EXAMPLE: f'images:\n  - newTag: "1.2.3"\n    name: {IMAGE}\n'}
    _only(check_pins(files, "1.2.3"), EXAMPLE)


def test_overlay_zero_indent_entry_is_still_checked() -> None:
    files = {**GOOD, EXAMPLE: f'images:\n- name: {IMAGE}\n  newTag: "1.2.3"\n'}
    _only(check_pins(files, "1.2.3"), EXAMPLE)


def test_overlay_images_key_the_parser_cant_follow_fails() -> None:
    files = {**GOOD, EXAMPLE: f'images: [{{name: {IMAGE}, newTag: "1.2.3"}}]\n'}
    _only(check_pins(files, "1.2.3"), EXAMPLE)


def test_swapped_key_order_reports_the_real_tag() -> None:
    files = {**GOOD, EXAMPLE: f'images:\n  - newTag: "1.2.3"\n    name: {IMAGE}\n'}
    problems = check_pins(files, "1.2.3")
    # The tag is read; only the (genuinely) missing digest is reported.
    assert problems == [f"{EXAMPLE}: missing or malformed sha256 digest (None)"]


def test_non_numeric_package_version_fails_loudly() -> None:
    assert any(p.startswith(PYPROJECT) for p in check_pins(GOOD, "1.3.0rc1"))
