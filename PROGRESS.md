# deal-hunter — Progress Log

A running, dated log of completed work. Append a new entry (newest at the top)
each time a spec reaches `Complete` or a meaningful milestone lands. This is the
project's memory between sessions — `session-start` surfaces the latest entry.

## 2026-10-15 — Adopted the spec-driven workflow

- Adopted the spec-workflow plugin (v1.17.0) with the **established** profile:
  the project holds API keys and a saved Facebook login and is deployed.
- Recorded CI's commands in `.claude/spec-workflow.json`: `uv run pytest -q`,
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
  `docker build`. No coverage threshold.
- Wrote `CLAUDE.md` and the `deal-hunter-conventions` skill from the code as it
  is (aimm wrapper, untrusted-browser threat model, public-repo rule).
- Seeded `BACKLOG.md` and the roadmap from open work; first spec:
  `pin-image-digest`.

Next: `/next-task` (pin-image-digest), then `/graduate` for the established
profile's merge-boundary hardening.

## 2026-10-15 — deal-hunter 0.2.0 (before the workflow)

- PR #1: nuc-hunter renamed to deal-hunter; `[[watch]]` tables for any item
  with `mini-pc` / `tiny-pc` presets; generic Compose and Kubernetes manifests
  in `deploy/`. Code review + security audit, fixes re-reviewed; 329 tests.
- Published `ghcr.io/coreyjonoliver-labs/deal-hunter:0.2.0@sha256:adf697e7…`
  with build provenance; the repo stays public.

<!--
Entry format — keep each entry skimmable:
## [YYYY-MM-DD] — <short title of what shipped>
- what was implemented / decided
- test + coverage result
- any decisions made beyond the spec, and follow-ups deferred to the backlog
-->
