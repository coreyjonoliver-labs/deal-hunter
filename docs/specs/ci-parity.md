# Spec: CI parity

**Status:** Complete
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-16
**Phase:** Phase 2 — Hardening
**Depends on:** graduate-established, ops-metrics

---

## Summary

Make the checks that only ran in one place run where they're needed: the
Prometheus alert-rule unit tests run in CI, and the CI secret scan can run
locally before a push.

## Goal

As a maintainer, I want CI to run every test the suite defines, and I want
CI's secret scan to catch a leaked or realistic-looking secret before the
first push. Then a failure doesn't need a history rewrite, and an
acceptance criterion never rests on a test that has only ever skipped.

## Background

- **The promtool tests have never run.**
  `tests/test_deploy.py::test_prometheus_rules_pass_their_unit_tests` skips
  without `promtool`, and CI doesn't install it (BACKLOG `promtool-in-ci`).
  On their first run (promtool 3.15.0, 2026-10-16) all eight
  alert-expecting cases failed. promtool compares annotations exactly, and
  `rules.test.yaml` listed none. The rule logic, labels and timing were
  right.
- **Secret-scan failures only show up in CI.** CI's `secret-scan` (gitleaks
  8.30.1, `security.yml`) scans every commit with a config from outside the
  checkout. Realistic fake keys in ops-metrics and otel-tracing fixtures
  failed it after both reviewers had passed the change, and only a
  force-push cleared it (lessons log 2026-10-16; curated rule 1).
- **gitleaks 8.30.1 fails open, in CI too.** It reports "no leaks found" and
  exits 0, scanning nothing or not seeing file contents, when:
  - git, which gitleaks runs, prints a warning (a committed `.gitattributes`
    with a negative pattern such as `!x foo`; a branch named like a 40-hex
    commit id);
  - git colours its output (`color.ui=always` in the user's config);
  - a committed `.gitattributes` marks the file `-diff` or `binary`, so git
    shows "Binary files differ";
  - git shows no diff for a merge commit's own changes, so a secret added
    while resolving a merge (`git merge --no-commit`, edit, commit) is never
    seen; a root commit is hidden by `log.showRoot=false`; and a replace ref
    can show a different commit for a pushed one.

  A PR can commit the `.gitattributes`, so the required `secret-scan` check
  could go green over a committed secret. Found in the round-2 review of this
  spec's hook; CI had the same flaw.

## Requirements

1. **Rule tests pass.** `rules.test.yaml` shall give `exp_annotations` for
   every expected alert. `promtool test rules` shall pass against the
   PrometheusRule in `prometheusrule.yaml`.
2. **No silent skip in CI.** When `DEAL_HUNTER_REQUIRE_PROMTOOL=1` is set,
   the promtool test shall fail if `promtool` is missing, not skip.
   Without the variable it still skips, for contributors who don't have
   promtool.
3. **CI installs promtool.** `build.yml`'s `test` job shall install
   promtool from the Prometheus release (linux-amd64, a pinned version and
   SHA-256 from the release's `sha256sums.txt`). It goes outside the
   checkout, with `DEAL_HUNTER_REQUIRE_PROMTOOL=1` set for `pytest`. This
   is a workflow change, so the operator applies it.
4. **Pre-push secret scan.** `scripts/git-hooks/pre-push` shall run gitleaks
   over the commits being pushed that the remote being pushed to doesn't
   already have. It uses CI's settings:
   - `useDefault` config and an empty ignore file, both written to a
     temporary directory (under `${TMPDIR:-/tmp}`, outside the checkout), so
     the repo's own `.gitleaks.toml` and `.gitleaksignore` are not read;
   - `--ignore-gitleaks-allow`, `--redact`, `--no-banner`, `--no-color`.

   The scan shall not be bypassable by the commits' content or the user's git
   config: gitleaks runs with `GIT_ATTR_SOURCE` set to the empty tree (git
   reads no committed `.gitattributes`; `--text` and the flags below keep local
   attribute files from hiding a diff), `GIT_NO_REPLACE_OBJECTS=1` (no replace refs),
   `core.warnAmbiguousRefs=false`, and `--log-opts "--no-color --text
   --no-textconv --no-ext-diff --root --diff-merges=first-parent <tips> --not
   --remotes=<remote>"` (a merge is scanned as its diff against its first
   parent, so edits made in the merge are seen; combined diffs are not used,
   because gitleaks parses them as nothing).

   What "not bypassable" covers: config, attributes, colour, merges, root
   commits and replace refs. gitleaks's default path allowlist (images,
   lockfiles such as `package-lock.json` and `go.sum`, ...) still applies
   through `useDefault`, in CI as well.

   It shall exit non-zero and block the push in any of these cases:
   - gitleaks exits non-zero (it found something);
   - gitleaks logs an `ERR` line;
   - gitleaks scanned no commits although the push changes files (a pushed
     commit with a file change, a merge counting by its diff against its
     first parent);
   - gitleaks is missing;
   - gitleaks's version differs from `security.yml`'s `GITLEAKS_VERSION`;
   - git can't list the commits, or a ref line has an object name that isn't
     hex.

   Details:
   - Branch deletions are skipped. The version and missing-gitleaks checks run
     only once a non-delete ref is found and the push has file changes, so a
     deletion-only, already-on-the-remote or empty-commit push, or a push of
     merges that change nothing against their first parent (`-s ours`), exits
     0 without gitleaks installed. A clean merge is scanned (its diff against
     its first parent) and passes.
   - "Already on the remote" means a remote-tracking ref of the remote named
     in the hook's first argument, which must match `^[A-Za-z0-9._-]+$`. A
     URL, or any other value, excludes nothing: everything reachable from the
     pushed refs is scanned.
   - A strict "N scanned == N pushed" check was tried and rejected: gitleaks
     counts only commits with a diff, so merge and empty commits make the
     numbers differ.
   - The hook is opt-in (`git config core.hooksPath scripts/git-hooks`), and
     `git push --no-verify` bypasses it.
5. **CI's scan can't be bypassed either.** The `gitleaks` step of
   `security.yml` shall run with `GIT_ATTR_SOURCE` set to the empty tree,
   `GIT_NO_REPLACE_OBJECTS=1` and `--log-opts "--all --full-history
   --no-color --text --no-textconv --no-ext-diff --root
   --diff-merges=first-parent"`, log to `$RUNNER_TEMP`, and fail unless gitleaks exits 0,
   logs no `ERR` line and scans at least one commit. This is a workflow
   change, so the operator applies it. `tests/test_ci_parity.py` runs the
   step's script from the workflow text against throwaway repos.
6. **The versions stay in step.** A test shall fail if:
   - the hook's gitleaks version differs from `security.yml`'s; or
   - `build.yml` doesn't install promtool, check its SHA-256 and set
     `DEAL_HUNTER_REQUIRE_PROMTOOL=1`.
7. **Docs.** `CONTRIBUTING`-style guidance in the README's development
   section shall cover enabling the hook and installing gitleaks. The
   conventions skill text, which the operator applies, shall carry:
   - the pending ops-metrics and otel-tracing additions (BACKLOG
     `conventions-ops-metrics`);
   - the pre-push scan and fixture rules;
   - a review checklist from the upheld lesson clusters.

## Acceptance Criteria

```gherkin
Given promtool 3.15.0 on PATH
When uv run pytest tests/test_deploy.py runs
Then test_prometheus_rules_pass_their_unit_tests passes

Given DEAL_HUNTER_REQUIRE_PROMTOOL=1 and no promtool on PATH
When the promtool test runs
Then it fails naming promtool, rather than skipping

Given a commit that adds a realistic fake API key, not yet on any remote
When the pre-push hook runs for that push
Then it exits non-zero and gitleaks reports the finding (redacted)

Given a gitleaks:allow comment on that line
When the pre-push hook runs
Then it still fails

Given a commit with the planted key and a .gitattributes with a negative
pattern, or `-diff`, or `binary`; or color.ui=always in the repo's config; or
a branch named like the commit's 40-hex id; or the key added only in a merge
commit's own changes (an evil merge); or log.showRoot=false on a root commit
with the key; or a replace ref hiding the commit
When the pre-push hook runs
Then it exits non-zero and gitleaks reports the finding

Given the same, including the ambiguous branch name (which can't warn there,
because the step passes no revs)
When the security.yml gitleaks step's script runs
Then it exits non-zero

Given a clean merge, or an empty `-s ours` merge, being pushed
When the pre-push hook runs
Then it exits 0 (the empty one without scanning)

Given gitleaks exits 0 but logs an ERR line, or scanned 0 commits, or no count
When the pre-push hook runs for a push that changes files
Then it exits non-zero

Given the commits being pushed are all on the pushed remote's tracking refs
When the pre-push hook runs
Then it exits 0 without scanning (a tracking ref of another remote doesn't count)

Given no gitleaks on PATH, or a different gitleaks version
When the pre-push hook runs
Then it exits non-zero and says how to install gitleaks <version>

Given the push only deletes a branch
When the pre-push hook runs
Then it exits 0 without scanning

Given build.yml's test job
When the parity test reads it
Then it installs promtool with a sha256sum check and sets DEAL_HUNTER_REQUIRE_PROMTOOL=1
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `deploy/kubernetes/components/prometheus/rules.test.yaml` | edit | `exp_annotations` |
| `tests/test_deploy.py` | edit | the require-promtool switch |
| `scripts/git-hooks/pre-push` | new | POSIX sh |
| `tests/test_ci_parity.py` | new | hook behaviour (stub gitleaks) and version parity |
| `.github/workflows/build.yml` | edit (operator) | install promtool |
| `.github/workflows/security.yml` | edit (operator) | the `gitleaks` step: empty-tree attributes, plain text, fail on error or nothing scanned |
| `README.md` | edit | enable the hook |
| `.claude/skills/deal-hunter-conventions/SKILL.md` | edit (operator) | conventions and review checklist |

## Data / State Impact

None.

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | `tests/test_ci_parity.py`, `tests/test_deploy.py` |
| Coverage stays at or above the project's threshold | N/A | no threshold |
| Failure paths and edge cases are tested | Yes | missing or wrong gitleaks, finding, allow comment, deletion |
| No secrets in code, config, tests, or fixtures | Yes | the hook test's planted key is built at runtime in a temp repo, never committed |
| External input is validated before use | Yes | the hook reads git's pre-push stdin; refs are passed as argv, never evaluated |
| No injection vector | Yes | no `eval`; quoted expansions |
| Errors fail closed and don't leak internals | Yes | missing or wrong scanner blocks; `--redact` |
| New dependencies justified and pinned | Yes | promtool 3.15.0, SHA-256 pinned, CI only; gitleaks is unchanged |
| Public interfaces documented; docs updated | Yes | README |

## Edge Cases

1. **A first push of a new branch** has no remote sha. The hook scans the
   commits not on any remote-tracking ref of that remote (`--not
   --remotes=<remote>`), so it never rescans `main`'s history.
2. **Stale remote-tracking refs** make it scan more, never less.
3. **The scanned tree ships a `.gitleaks.toml` or `.gitleaksignore`.** They
   are not used, because the config and ignore path come from the temporary
   directory.

## Testing Strategy

- **Unit:** the hook is run against throwaway git repos, with a stub gitleaks
  on PATH (for argv, version and missing cases). The real gitleaks runs
  when it is on PATH (skip otherwise).
- **Parity:** parse `security.yml` and `build.yml` as text.

## Out of Scope

- Running CodeQL locally.
- Adding promtool to the dev environment.
- A Claude Code hook. The operator pushes with `!`, so a git hook is the one
  that fires.

## End-to-End Verification

```sh
PATH=<promtool dir>:$PATH uv run pytest -q tests/test_deploy.py
uv run pytest -q tests/test_ci_parity.py
git config core.hooksPath scripts/git-hooks && git push   # with gitleaks 8.30.1 installed
```

## Open Questions

- None.
