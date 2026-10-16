# deal-hunter — Roadmap

This roadmap is a **table of contents that points at specs**. It defines phase
sequencing and inter-spec dependencies — nothing more. The specs in
`docs/specs/` are the single source of truth for scope, requirements, and
acceptance criteria. If this file and a spec disagree, the spec wins.

Keep the rows in sync with reality: update the **Status** column when a spec
moves, and add a row when you write a new spec.

Status vocabulary: `—` (not started) · `Draft` · `Ready` · `In Progress` ·
`Complete`.

## Phase 1 — Release hygiene

Objective: what `deploy/` ships is exactly what was reviewed and published.

| Spec | Depends on | Status |
|---|---|---|
| [pin-image-digest](specs/pin-image-digest.md) | None | Complete |
| [live-watch-smoke-test](specs/live-watch-smoke-test.md) | None | Complete |
| [investigate-search-failures](specs/investigate-search-failures.md) | None | Complete |
| [rating-summary-format](specs/rating-summary-format.md) | None | Complete |
| [release-0-3-0](specs/release-0-3-0.md) | None | Complete |
| bump-aimm (deferred) | None | — |
| immutable-release-tags | None | — |

## Phase 2 — Hardening

Objective: the established profile's merge boundary, and a narrower blast
radius for a compromised browser.

| Spec | Depends on | Status |
|---|---|---|
| [graduate-established](specs/graduate-established.md) | None | Complete |
| scoped-agent-token | graduate-established | — |
| chromium-sandbox-seccomp (Research) | None | — |
| dashboard-own-pod | chromium-sandbox-seccomp | — |

## Phase 3 — Alerting & observability

Objective: the buyer hears about good deals where they already look. For now
alerting is scoped to that (operator decision, 2026-10-15); the operator-health
specs (ops-metrics, otel-tracing) were written and deferred, then picked up
on 2026-10-16 (operator decision: ops-metrics first, then otel-tracing).

| Spec | Depends on | Status |
|---|---|---|
| [discord-deal-alerts](specs/discord-deal-alerts.md) | None | Complete |
| [ops-metrics](specs/ops-metrics.md) | None | Complete |
| [otel-tracing](specs/otel-tracing.md) | ops-metrics | Complete |

<!--
Add phases as the project grows. A phase is just a grouping for sequencing; the
contracts are the specs it lists. For a small project, one phase is fine.
-->
