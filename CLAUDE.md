<!--
  CLAUDE.md is loaded into EVERY session, so keep it lean. Detail belongs in the
  project's conventions skill, which loads on demand. For each line here ask:
  "would removing this cause Claude to make a mistake?" If not, cut it. A bloated
  CLAUDE.md causes the model to ignore the rules that matter.
-->

# deal-hunter — Project Intelligence

Watch Facebook Marketplace for what you want, rated by AI.

Claude Code reads this file at the start of every session. It is the map: the
settled decisions and the few rules that govern every change. The **manual** —
testing, linting, security, error-handling, and task procedures in detail — is
the project's conventions skill (the `.claude/skills/*-conventions/` SKILL.md for
the stack in scope). Claude loads it by description when you work in this stack;
load it before implementing or reviewing anything. Projects with more than one
language have one such skill per language, each loaded on demand.

## Overview

A thin, security-hardened front-end for
[ai-marketplace-monitor](https://github.com/BoPeng/ai-marketplace-monitor)
("aimm"): it turns a short `deal-hunter.toml` into an aimm config that watches
Facebook Marketplace for the things a person wants (`[[watch]]` tables, with
presets like `mini-pc`), has an AI rate each listing, and notifies on good
deals. `deal-hunter web` is a read-only dashboard. Self-hosters run it on a
desktop, in Docker, or on Kubernetes (`deploy/`). **The repo is public.**

## Tech stack

- **Language:** Python
- **Type:** CLI + service
- **Python 3.10–3.13**, managed with **uv** (`uv.lock`; `uv sync --locked`).
- **ai-marketplace-monitor, pinned exactly** — does the browsing (Playwright
  Chromium), de-duplication (a diskcache SQLite cache), AI calls and
  notifications. deal-hunter generates its config and patches it at start.
- **Dashboard:** stdlib `http.server`, server-rendered HTML, no JavaScript.
- **Tracing (optional):** OpenTelemetry traces over OTLP/HTTP from `run`
  (`tracing.py`; the `tracing` extra, installed in the image), off unless
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set. A span processor allowlists what leaves:
  no listing text, phrase, location, URL or exception message, and no trace
  context on outbound requests.
- **Image:** `Dockerfile` (uid 1000, read-only root), published to GHCR with
  SLSA build provenance by `.github/workflows/build.yml`.

## Architecture decisions

The settled choices. Follow them; don't relitigate them in a change.

1. **Wrap aimm, don't fork it.** Generate its config (`render.py`) and patch
   the few behaviors that are unsafe or broken (`launch.py`), each with a
   patch-point test, against an exact pinned release.
2. **No secrets in settings or rendered config** — only `${ENV_VAR}`
   placeholders aimm expands at runtime.
3. **The browser is untrusted.** In containers it runs without Chromium's
   sandbox, so aimm's home directory and everything the monitor writes are
   treated as attacker-controlled by the dashboard and by `run` itself.
4. **Old settings keep working.** Legacy `[search]`-only settings keep their
   item names (`nuc` / `tiny_pc`) and render the same structure, because aimm
   keys cached ratings by item name. Prompt text may change deliberately; that
   re-rates listings as they're seen again (aimm's cache key includes the item
   config's hash), so a prompt change says so in its PR.
5. **Generic deployment lives here; personal deployment doesn't.** `deploy/`
   is for any self-hoster; nothing specific to one person's location or
   cluster is ever committed.
6. **Configuration is code; data goes in a database only when a feature
   needs it.** Intent (watches, budgets, thresholds, AI, notification routing,
   schedules) lives in the TOML, validated and reviewed in git — no UI or bot
   writes it. Secrets come only from env; process wiring (ports, hosts,
   paths, endpoints) from flags and env. A deal-hunter-owned database is added
   only with the first feature that needs runtime data, and runtime data may
   only narrow behavior (dismiss, mute) and must expire or be visible — it
   never redefines intent. The first input channel, when one is needed, is the
   dashboard (which makes `web` read-write: writable volume, CSRF, trusted
   identity). Detail: the conventions skill, "Configuration model".
7. **Specs are the source of truth.** Every non-trivial unit of work is defined
   by exactly one spec in `docs/specs/`. The roadmap sequences specs; it is not a
   competing plan.

## Coding standards (the must-knows)

Full detail is in the project's conventions skill. The essentials:

- **Test-first.** Red → green → refactor, against aimm's real code (its
  `Config`, its keyword matcher), never a mock of it.
- **Lint and types clean** before review.
- **Errors fail loudly and specifically** — no swallowed exceptions, no ignored
  error returns.
- **No secrets in the repo.** Validate external input. (See the skill's Security
  section.)
- **Match the surrounding code**; copy the nearest example over inventing a style.

## Operating profile

**Operating profile:** established

This sets how hard verification gates the workflow. **greenfield** is the fast,
low-ceremony loop for pre-production work; **established** adds merge-boundary
hardening (required CI gate, branch protection, CODEOWNERS, stricter hooks) for
load-bearing code. The rules tighten on graduation — run `/graduate` when this
project gets real users/data, handles money/auth/secrets/PII, or becomes
long-term-maintained. See the spec-workflow plugin's `docs/profiles.md`. For
OS-enforced isolation — the boundary the guard hooks point at, and the rule
that **unattended runs need a whole-process sandbox** — see the plugin's
`docs/sandboxing.md`. (The plugin's workflow docs aren't copied into this
public repo.)

**Merge boundary.** `main` takes changes only through a PR, merged with a
merge commit, once conversations are resolved and the required checks pass:
`test` and `image` (`build.yml`), `secret-scan`, `dependency-review` and
`codeql` (`security.yml`), all from GitHub Actions. New CodeQL alerts block
through the `main-code-scanning` ruleset. Admins are bound too. A PR runs its
own copy of the workflows, so **a PR that touches `.github/workflows/**` sets
its own checks: read that diff by hand before merging.** Publishing isn't
covered: any branch whose workflow publishes (manual dispatch, or an edited
push trigger) can still publish over the version tag until
`immutable-release-tags`. The agent's `gh` runs unsandboxed with the
operator's token; the `permissions.ask` prompts on merge, publish and settings
subcommands are a speed bump, not a boundary (that's `scoped-agent-token`).
`.github/CODEOWNERS` names the load-bearing paths, and
`tests/test_codeowners.py` keeps them existing. The agent can't write
`.github/workflows/**` or `.claude/settings.json`; it hands the operator the
exact content to apply.

## Quality gate

A change is **not done** until the review gate passes. The bar:

- The project's **test** command passes (no coverage threshold is set).
- The project's **lint** and **format** commands are clean.
- The project's **typecheck** command reports no type errors.
- The **build** command succeeds when the image's inputs changed.

The canonical commands and threshold live in `.claude/spec-workflow.json`
(`commands.*`, `coverageThreshold`), surfaced in the session-start "Project
verification" context block. Show the command output rather than asserting success.

## Spec workflow

Every unit of work follows the same loop:

1. **Spec** — write/update `docs/specs/<name>.md` from `docs/specs/_template.md`
   (`/spec <name>`). The spec owns scope, requirements, and acceptance criteria.
   For a batch — an MVP or a set of features — `/plan <description>` interviews
   you and decomposes prose goals into several sequenced specs.
2. **Execute** — `/review-gate <name>` runs implementer → code-reviewer →
   security-auditor (when in scope) → fix loop until zero CRITICAL/HIGH. For a
   one-sentence change, `/implement-and-review`. For a Research spec, `/research`.
3. **Track** — on a green gate: set the spec `Complete`, update the roadmap row,
   tick `BACKLOG.md`, append a dated `PROGRESS.md` entry.

`/next-task` drives this end to end against the backlog.

## Workflow invariant

Marking a spec `Complete`, ticking the backlog, or writing a `PROGRESS.md` entry
**before the gate has run and passed** is a process violation, not a shortcut.
The only sanctioned skip is the sub-agent *dispatch* on a change with nothing in
its domain (a doc-only or test-only diff) — the gate itself always runs, and the
skip is stated with its reason.

This invariant is **mechanized by the gate stamp**: on a passing verdict,
`/review-gate` writes `.claude/gate/<spec-name>.json` (recording the resolved
review roster, checks mode, and verdict). The edit guard (`edit-write-guard.sh`)
**denies** any Edit/Write that sets a spec's `**Status:**` to `Complete` unless
that stamp exists. The guard fails *closed*: a missing stamp, an unreadable gate
directory, or any ambiguity about the spec name all result in a deny, never a
silent allow. Non-`Complete` status values are never gated.

## What NOT to do

- Don't implement without a spec for anything non-trivial.
- Don't mark work done without running the gate.
- Don't commit secrets — use environment / the project's secrets mechanism.
- Don't expand a change beyond its spec; note adjacent issues in `BACKLOG.md`.
- Don't let `CLAUDE.md` grow stale — if a change introduces a new convention or
  structure, update this file and the skill in the same change.

## Tracking files

- **`BACKLOG.md`** — the queue (Current Focus / Up Next / Later).
- **`PROGRESS.md`** — the dated log of what's been completed.
- **`docs/roadmap.md`** — phase sequencing / dependency map (points at specs).

Consult them before starting; update them as work moves.
