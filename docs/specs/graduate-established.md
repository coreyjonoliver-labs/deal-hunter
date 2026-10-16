# Spec: Graduate to the established profile

**Status:** Complete
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-15
**Phase:** Phase 2 — Hardening
**Depends on:** None

---

## Summary

Make `main`'s merge boundary real on the server: required checks (tests, image
build, secret scan, dependency review, CodeQL), branch protection, CODEOWNERS
over the load-bearing paths, and a sandbox that can't silently fall back to
unsandboxed.

## Goal

As the maintainer of a public repo whose image self-hosters run next to their
API keys and Facebook session, I want every change to `main` to pass the same
deterministic checks through a PR, so that nothing — mine, an agent's or a
contributor's — lands unchecked.

## Background

`CLAUDE.md` already says `established`, but only the local side exists: the
review gate, guard hooks (from the spec-workflow plugin) and the Bash sandbox.
On GitHub, `main` is unprotected (`branches/main/protection` → 404, no
rulesets), there's no CODEOWNERS, and nothing scans for secrets or vulnerable
dependencies. `build.yml`'s `test` job already runs the project's checks on
every PR, so the plugin's `agent-gate.yml` `checks` job would duplicate it.

The guard chain denies the agent `.github/workflows/**` and
`.claude/settings.json`, so the operator applies those pieces; the agent writes
the rest and the exact content to apply.

Operator decisions (2026-10-15): keep merge commits (no linear-history rule);
deterministic CI only, no model review in CI (the local gate already reviews);
CODEOWNERS covers the controls plus `deploy/`, `Dockerfile`, the dependency
pins, `launch.py`, `discord.py` and the dashboard; no Stop-hook gate for now.

## Requirements

1. `.github/CODEOWNERS` shall name the operator for `.github/`, `.claude/`
   (settings, gate stamps, lessons, conventions skill), `deploy/`, `Dockerfile`,
   `pyproject.toml`, `uv.lock`, `src/deal_hunter/launch.py`,
   `src/deal_hunter/discord.py` and the dashboard (`src/deal_hunter/dashboard.py`,
   `src/deal_hunter/dashboard.html`).
2. A test shall fail when a CODEOWNERS path doesn't exist in the repo with
   exactly that case (a rename would otherwise silently drop ownership; GitHub
   matches case-sensitively), when a rule ending in `/` isn't a directory, or
   when a line has no owner.
3. A new workflow `.github/workflows/security.yml` (operator-applied) shall run
   on PRs to `main` and pushes to `main`, with jobs `secret-scan` (gitleaks
   8.30.1 CLI, checksum-verified, full history, with a config and an empty ignore
   file written by the workflow and `--ignore-gitleaks-allow`, so the scanned tree
   can't allowlist itself), `dependency-review` (PRs only, fail on high) and
   `codeql` (python and actions). Every action shall be pinned to a full commit
   SHA of a release at least 5 days old, with `persist-credentials: false` and
   least-privilege `permissions`.
4. Branch protection on `main` (operator-applied) shall require the checks
   `test`, `image`, `secret-scan`, `dependency-review` and `codeql`, each
   pinned to the GitHub Actions app (`app_id` 15368, so no other app or status
   can satisfy them), up to date with `main`; `enforce_admins`; a PR for every
   change with zero required approvals (solo maintainer) and conversations
   resolved; no force pushes or deletions; merge commits allowed.
4a. A ruleset `main-code-scanning` on the default branch (operator-applied)
   shall block a merge on new CodeQL alerts (security high or higher, or
   errors). The `codeql` job alone only proves the analysis ran.
5. The repo's dependency graph, secret scanning and push protection (free on
   public repos) shall be enabled, and Actions shall require approval before running workflows from
   any external contributor (operator-applied).
6. `.claude/settings.json` (operator-applied) shall add
   `sandbox.failIfUnavailable: true` and `sandbox.allowUnsandboxedCommands:
   false`, and `permissions.ask` rules on the `gh` subcommands that can merge,
   publish, change settings or reveal the token (`gh api`, `auth`, `alias`,
   `extension`, `pr merge`, `workflow`, `run`, `release`, `secret`,
   `variable`, `repo`, `ruleset`, and global `-R`/`--repo`). This is a speed
   bump against an accidental action, not a boundary (edge case 6).
7. `CLAUDE.md` shall state the merge boundary: PR-only `main`, the required
   checks, and that `.github/workflows/**` and settings changes are
   operator-applied.

## Acceptance Criteria

```gherkin
Given the CODEOWNERS file
When the test suite runs
Then every owned path exists and every rule has an owner

Given a CODEOWNERS rule whose path was renamed away
When the test suite runs
Then the CODEOWNERS test fails naming that path

Given the operator applied the checklist
When gh api repos/:owner/:repo/branches/main/protection is read
Then the required checks are test, image, secret-scan, dependency-review, codeql, each with app_id 15368, strict is true and enforce_admins is true

Given a PR that adds a .gitleaks.toml allowlisting everything and a secret marked gitleaks:allow
When secret-scan runs
Then it fails (verified locally with gitleaks 8.30.1 and the workflow's flags)

And conversation resolution is required, force pushes and deletions are off, and 0 approvals are required

Given the operator applied the checklist
When the main-code-scanning ruleset's rules are read
Then it has a code_scanning rule for CodeQL at high_or_higher / errors, enforcement active, no bypass actors

Given the operator applied the checklist
When the repo's security_and_analysis is read
Then secret_scanning and secret_scanning_push_protection are enabled, and the fork-PR approval policy is all_external_contributors

Given the operator applied the settings delta
When .claude/settings.json is read
Then sandbox.failIfUnavailable is true, sandbox.allowUnsandboxedCommands is false, and permissions.ask lists the req 6 subcommands

Given a PR to main
When its checks run
Then secret-scan, dependency-review and codeql report alongside test and image
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `.github/CODEOWNERS` | new | agent-written (not a guarded path) |
| `tests/test_codeowners.py` | new | paths exist, owners present |
| `.github/workflows/security.yml` | new | operator-applied (guarded path) |
| `.claude/settings.json` | edit | operator-applied (guarded path) |
| `CLAUDE.md` | edit | merge boundary note |
| GitHub settings | admin | branch protection, `main-code-scanning` ruleset, secret scanning, fork-PR approval (operator) |

## Data / State Impact

None. After protection is on, every change to `main` goes through a PR.

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | `tests/test_codeowners.py`; protection read back with `gh api` |
| Coverage stays at or above the project's threshold | N/A | no threshold |
| Failure paths and edge cases are tested | Yes | missing path, wrong case, file named as a directory, ownerless rule; a self-allowlisting PR against gitleaks |
| No secrets in code, config, tests, or fixtures | Yes | gitleaks over full history: no leaks (19 commits) |
| External input is validated before use | N/A | |
| No injection vector | Yes | the workflow interpolates no PR-controlled values into `run:` |
| Errors fail closed and don't leak internals | Yes | checksum mismatch fails the job; sandbox can't fall back |
| New dependencies justified and pinned | Yes | actions SHA-pinned, gitleaks checksum-pinned |
| Public interfaces documented; docs updated | Yes | `CLAUDE.md` |

## Edge Cases

1. `dependency-review` only runs on PRs → required only as a PR check; on a
   push to `main` the job is skipped, which branch protection doesn't evaluate.
2. A solo maintainer can't approve their own PR → zero required approvals;
   `enforce_admins` still makes the checks binding.
3. A PR from before `security.yml` exists won't report the new checks → re-run
   or push to it after the workflow lands.
4. **A PR defines its own checks.** `pull_request` runs the workflow files from
   the PR, so a PR that edits `security.yml` or `build.yml` can make its own
   required checks pass vacuously; with zero approvals, nothing else stops it.
   Accepted residual for a solo repo: such a diff is read by hand before
   merging (CLAUDE.md says so), and the agent's usual merge spellings prompt
   (req 6, a speed bump; see edge case 6). The
   real fix — org "required workflows" that run from `main` — is out of scope.
5. **Publishing is outside branch protection.** Any branch whose workflow
   publishes — `build.yml`'s `workflow_dispatch`, or a pushed branch that
   edits `build.yml`'s `push:` trigger — builds, publishes over the version tag
   and attests (signed as that branch), with no PR involved. Strict verify
   (`--source-ref refs/heads/main`) and digest pins reject such an image;
   tag-only pulls don't. A `github.ref` check inside `build.yml` can't fix it
   (the branch edits that file too); `immutable-release-tags` needs a control
   outside the workflow (an environment-scoped publish credential limited to
   `main`, or registry-side immutable tags).
6. **The agent's `gh` holds the operator's admin token.** `gh` and `docker`
   stay in `sandbox.excludedCommands` (both are incompatible with it), so they
   are the unsandboxed surface, and the admin token can change protection,
   dispatch workflows and merge. Req 6's prompts catch the usual spellings, but
   pattern rules can be routed around (e.g. `curl` to the API with a token).
   The boundary is a token that can't do those things: BACKLOG
   `scoped-agent-token` (a fine-grained `GH_TOKEN` for agent sessions with
   contents and pull-request write, no Administration or Actions write).
7. **The dependency graph needed turning on.** It was off (the first
   `dependency-review` run failed: "Dependency graph is not enabled"). Once the
   operator enabled it, GitHub listed `uv.lock` as a parsed manifest and the
   SBOM had 120 packages (114 in `uv.lock`, plus the actions and the root),
   so transitive dependencies are reviewed.
8. **A gitleaks false positive** can't be suppressed from the repo (the scan
   ignores `.gitleaks.toml`, `.gitleaksignore` and `gitleaks:allow` by
   design). The operator adds an allowlist entry (a finding fingerprint or a
   path) to the config `security.yml` writes, and reads that diff by hand as
   for any workflow change (edge case 4).
9. **Fork PRs and `codeql`.** A fork's token is read-only, so the SARIF upload
   may fail and the required `codeql` check go red for an outside
   contributor. Accepted for a solo repo: the maintainer re-runs the change
   from a branch.

## Testing Strategy

- **Unit:** `tests/test_codeowners.py`.
- **Manual:** gitleaks over the history, and against a probe repo whose
  `.gitleaks.toml`, `.gitleaksignore` and `gitleaks:allow` try to hide a key
  (default flags: no leaks; the workflow's flags: 1 leak); read back branch
  protection and the ruleset; one PR showing all five checks.

## Out of Scope

- A model review in CI (`agent-review.sh`) — declined.
- The Stop-hook gate and interpreter lockdown — declined for now; the guard
  hooks live in the plugin.
- Immutable release tags, gating publish on `refs/heads/main` and verifying
  pins in CI (their own backlog items).
- Org "required workflows" rulesets that run checks from `main` rather than
  the PR.

## End-to-End Verification

```sh
uv run pytest -q tests/test_codeowners.py
gh api repos/coreyjonoliver-labs/deal-hunter/branches/main/protection \
  --jq '{checks: .required_status_checks.checks, strict: .required_status_checks.strict, admins: .enforce_admins.enabled, approvals: .required_pull_request_reviews.required_approving_review_count}'
gh api repos/coreyjonoliver-labs/deal-hunter/branches/main/protection \
  --jq '{conversations: .required_conversation_resolution.enabled, force: .allow_force_pushes.enabled, deletions: .allow_deletions.enabled}'
gh api repos/coreyjonoliver-labs/deal-hunter/rulesets --jq '.[] | {id, name, enforcement}'
gh api repos/coreyjonoliver-labs/deal-hunter/rulesets/<id> --jq '{enforcement, bypass: .bypass_actors, rules}'
gh api repos/coreyjonoliver-labs/deal-hunter --jq '.security_and_analysis | {secret_scanning, secret_scanning_push_protection}'
gh api repos/coreyjonoliver-labs/deal-hunter/actions/permissions/fork-pr-contributor-approval
jq '{sandbox: (.sandbox | {failIfUnavailable, allowUnsandboxedCommands}), ask: .permissions.ask}' .claude/settings.json
gh pr checks <the PR that lands this>
```

## Open Questions

- None.
