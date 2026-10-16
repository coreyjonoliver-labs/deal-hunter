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
import shutil
import subprocess
from pathlib import Path

import pytest

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


def _to_element_keys(text: str, start: str) -> dict[str, str]:
    """The selectors in one NetworkPolicy `to` element, each with its first matchLabels line.

    The element starts at the line holding `start` and ends at the first line indented
    less than its keys. A selector belongs to it only at the keys' own indent, so two
    selectors returned together are ANDed. A line scan, not a regex, so it stays linear.
    """
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if line.strip() == start.strip())
    indent = lines[first].index(start.lstrip("- ").strip())
    keys: dict[str, str] = {}
    key = ""
    for line in [lines[first].replace("- ", "  ", 1), *lines[first + 1 :]]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        col = len(line) - len(line.lstrip())
        if col < indent:
            break
        body = line.split("#", 1)[0].strip()
        if col == indent:
            key = body.rstrip(":")
        elif key and key not in keys and body != "matchLabels:":
            keys[key] = body
    return keys


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


# --- The Prometheus component ---------------------------------------------------

PROMETHEUS = ROOT / "deploy/kubernetes/components/prometheus"
ALERTS = [
    "DealHunterSearchStalled",
    "DealHunterSessionExpiringSoon",
    "DealHunterSessionExpired",
    "DealHunterSearchFailures",
    "DealHunterAISpendHigh",
]


def rules_file(prometheusrule: str) -> str:
    """The PrometheusRule's `spec:` as a plain Prometheus rules file (dedented by two)."""
    _, found, spec = prometheusrule.partition("\nspec:\n")
    assert found, "no spec: in prometheusrule.yaml"
    return "".join(line[2:] if line.startswith("  ") else line for line in spec.splitlines(True))


def test_prometheus_rules_define_the_alerts_once_each() -> None:
    rules = rules_file((PROMETHEUS / "prometheusrule.yaml").read_text())
    assert rules.startswith("groups:\n")
    assert re.findall(r"^\s*- alert: (\w+)$", rules, re.MULTILINE) == ALERTS
    # Severity is the only label a rule sets: nothing from a listing, phrase or location.
    labels = re.findall(r"^\s+labels:\n((?:\s+\w+: .*\n)+)", rules, re.MULTILINE)
    assert len(labels) == len(ALERTS)
    assert all(re.fullmatch(r"\s+severity: (critical|warning)\n", block) for block in labels)
    # The spend threshold is set in one place, next to its comment.
    assert len(re.findall(r"> 1\.00$", rules, re.MULTILINE)) == 1
    assert "THE KNOB" in rules


def test_prometheus_component_exposes_only_the_metrics_port() -> None:
    kustomization = (PROMETHEUS / "kustomization.yaml").read_text()
    assert "path: /spec/template/spec/containers/1/args/-" in kustomization
    assert kustomization.count('value: "9741"') == 1 and "port: 9741" in kustomization
    assert 'deal-hunter/metrics-access: "true"' in kustomization
    assert "deal-hunter/dashboard-access" not in kustomization
    assert "port: 8740" not in kustomization  # the dashboard port gets no new rule
    assert "kind: ServiceMonitor" in (PROMETHEUS / "servicemonitor.yaml").read_text()


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool is not installed")
def test_prometheus_rules_pass_their_unit_tests(tmp_path: Path) -> None:
    (tmp_path / "rules.yaml").write_text(
        rules_file((PROMETHEUS / "prometheusrule.yaml").read_text())
    )
    (tmp_path / "rules.test.yaml").write_text((PROMETHEUS / "rules.test.yaml").read_text())
    promtool = shutil.which("promtool") or "promtool"
    for args in (["check", "rules", "rules.yaml"], ["test", "rules", "rules.test.yaml"]):
        done = subprocess.run(  # noqa: S603 — fixed argv
            [promtool, *args], cwd=tmp_path, capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.skipif(shutil.which("kustomize") is None, reason="kustomize is not installed")
def test_example_overlay_renders_the_prometheus_component() -> None:
    kustomize = shutil.which("kustomize") or "kustomize"
    done = subprocess.run(  # noqa: S603 — fixed argv
        [kustomize, "build", str(ROOT / "deploy/kubernetes/example")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "kind: ServiceMonitor" in out and "kind: PrometheusRule" in out
    assert "alert: DealHunterSearchStalled" in out
    # The overlay's own args replace the component's, so it must carry the flag
    # whenever the Service exposes the port, or the scrape would find nothing.
    assert re.search(r"- --metrics-port\n\s+- \"9741\"", out)
    assert "name: metrics\n    port: 9741" in out
    assert out.count("deal-hunter/metrics-access") == 1


def test_prometheus_patch_index_is_the_web_container() -> None:
    lines = (ROOT / DEPLOYMENT).read_text().splitlines()
    start = lines.index("      containers:")
    names = []
    for line in lines[start + 1 :]:
        if re.match(r"^      \S", line):  # the next key of the pod spec
            break
        if m := re.match(r"^        - name: (\S+)$", line):
            names.append(m.group(1))
    assert names.index("web") == 1
    assert "containers/1/" in (PROMETHEUS / "kustomization.yaml").read_text()


# --- The tracing component --------------------------------------------------------

TRACING = ROOT / "deploy/kubernetes/components/tracing"


def test_tracing_component_sets_only_the_endpoint_on_the_monitor_container() -> None:
    text = (TRACING / "kustomization.yaml").read_text()
    assert "kind: Component" in text
    # Matched by container name, not index, so the monitor stays the target if the order moves.
    assert "- name: monitor" in text and "containers/" not in text
    assert text.count("- name: OTEL_") == 1 and "name: OTEL_EXPORTER_OTLP_ENDPOINT" in text
    # No credential, header or sampler is baked in; the token comes from the Secret.
    assert "name: OTEL_EXPORTER_OTLP_HEADERS" not in text
    assert "value: http://otel-collector." in text  # a generic placeholder, edited per cluster
    # One egress rule, to the collector's port only; nothing is opened on the pod.
    assert text.count("path: /spec/egress/-") == 1 and "port: 4318" in text
    assert "ingress" not in text.split("patches:")[1]
    # The rule names the collector's pods, not the whole namespace: namespaceSelector and
    # podSelector are one `to` element (same indent), so they AND together.
    assert _to_element_keys(text, "- namespaceSelector:") == {
        "namespaceSelector": "kubernetes.io/metadata.name: observability",
        "podSelector": "app.kubernetes.io/name: opentelemetry-collector",
    }
    assert "use https" in text
    # The label is listed among the things to edit, with the consequence of getting it wrong.
    edit_section = text.split("apiVersion:")[0]
    assert (
        "podSelector" in edit_section and "silently blocks export" in edit_section
    )  # the plain-http placeholder says when not to


def test_tracing_is_not_in_the_example_overlay() -> None:
    # Tracing is opt-in: copying the example must not point a pod at a collector that isn't there.
    example = (ROOT / "deploy/kubernetes/example/kustomization.yaml").read_text()
    active = [line for line in example.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in active if "components/tracing" in line]
    assert "components/tracing" in example  # but the way to turn it on is shown


@pytest.mark.skipif(shutil.which("kustomize") is None, reason="kustomize is not installed")
def test_tracing_component_renders_after_cilium_egress(tmp_path: Path) -> None:
    kustomize = shutil.which("kustomize") or "kustomize"
    shutil.copytree(ROOT / "deploy/kubernetes", tmp_path / "k8s")
    overlay = tmp_path / "k8s" / "overlay"
    overlay.mkdir()
    (overlay / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
        "resources:\n  - ../base\n"
        "components:\n  - ../components/cilium-egress\n  - ../components/tracing\n"
    )
    done = subprocess.run(  # noqa: S603 — fixed argv
        [kustomize, "build", str(overlay)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in out and "port: 4318" in out
    assert "app.kubernetes.io/name: opentelemetry-collector" in out
    # On the monitor container, not the dashboard's.
    assert out.index("OTEL_EXPORTER_OTLP_ENDPOINT") < out.index("name: web")
