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
| [pin-image-digest](specs/pin-image-digest.md) | None | Ready |
| immutable-release-tags | None | — |

## Phase 2 — Hardening

Objective: the established profile's merge boundary, and a narrower blast
radius for a compromised browser.

| Spec | Depends on | Status |
|---|---|---|
| graduate-established | None | — |
| chromium-sandbox-seccomp (Research) | None | — |
| dashboard-own-pod | chromium-sandbox-seccomp | — |

<!--
Add phases as the project grows. A phase is just a grouping for sequencing; the
contracts are the specs it lists. For a small project, one phase is fine.
-->
