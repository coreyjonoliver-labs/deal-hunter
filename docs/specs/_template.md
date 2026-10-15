# Spec: [Name]

**Status:** Draft | Ready | In Progress | Implemented | Complete | Superseded
<!-- Lifecycle: Draft → Ready → In Progress → (Implemented) → Complete → Superseded (terminal).
     Complete = shipped and still the current contract.
     Superseded = replaced/removed/rewritten by a later spec; file moves to docs/specs/archive/.
     Non-Complete statuses are never gated. See the spec-workflow plugin's docs/spec-lifecycle.md. -->
**Kind:** Implementation | Research
**Author:** [name]
**Date:** [YYYY-MM-DD]
**Phase:** [e.g. Phase 2 — Core] (optional)
**Depends on:** [spec names whose Status must be Complete first, or "None"]

---

## Summary

One sentence: what this spec delivers and why it matters.

## Goal

- **Implementation:** As a [user / role], I want [capability], so that [benefit].
- **Research:** This spec produces [knowledge artifact] so that [which downstream
  specs it unblocks].

## Background

2–3 paragraphs of context. What prior work feeds in? Why now? What constraint or
decision in `CLAUDE.md` shapes this?

## Requirements

Numbered, specific, and **testable**. Prefer EARS phrasing — each requirement
should collapse to a single checkable claim:

- *Ubiquitous:* The system shall [always-true property].
- *Event-driven:* When [trigger], the system shall [response].
- *State-driven:* While [state], the system shall [response].
- *Unwanted:* If [condition], then the system shall [response].

1. [Requirement]
2. …

## Acceptance Criteria

**Implementation** — `Given / When / Then`, each mapping to at least one test:

```gherkin
Given [precondition]
When [action]
Then [observable outcome]
```

**Research** — deliverable criteria with a quality bar:

- [ ] [e.g. "Synthesis covers ≥ 5 primary sources per topic."]
- [ ] [e.g. "Every claim cites author, year, title."]
- [ ] [e.g. "Gaps section names ≥ 3 open questions."]

## [Implementation] Interfaces & Files

Name the files created / modified / deleted and the public interfaces (functions,
endpoints, types, CLI commands) this introduces or changes. Reviewers and the
implementer both work from this list.

| File | Change | Notes |
|---|---|---|
| `path` | create / edit / delete | what and why |

## [Implementation] Data / State Impact

| Entity / Store | Change | Details |
|---|---|---|
| [name] | create / read / update / delete / migrate | fields, indices, migration |

State "None" if the change touches no persistent state.

## [Research] Authoritative Sources

- [Source — author, title, year, URL if available]
- …

> Do not invent sources. If one is inaccessible, find the closest credible
> equivalent and note the substitution.

## [Research] Output Specification

- **Primary deliverable:** [e.g. `docs/research/<name>.md`]
- **Structured artifact (if any):** [e.g. `config/taxonomy.yaml`, a JSON Schema]
- **Format requirements:** [organization, citation style, required sections]

## Quality & Security Checklist

The review gate verifies these. Mark each Yes / No / N/A with a note.

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | | |
| Coverage stays at or above the project's threshold | | |
| Failure paths and edge cases are tested | | |
| No secrets in code, config, tests, or fixtures | | |
| External input is validated before use | | |
| No injection vector (SQL / command / path / deserialization) | | |
| Errors fail closed and don't leak internals | | |
| New dependencies justified and pinned | | |
| Public interfaces documented; docs updated | | |
| [project-specific rule — add rows as needed] | | |

## Edge Cases

1. [Edge case → handling]
2. …

## Testing Strategy

- **Unit:** [behaviors to test]
- **Integration:** [data flows / boundaries to verify]
- **Golden-file / snapshot:** [known-input → expected-output fixtures, if any]
- **Manual:** [what needs human verification, if anything]

## Out of Scope

- [Explicitly excluded — prevents scope creep during implementation.]

## End-to-End Verification

The single sequence that proves the feature works once implemented — the commands
to run and the expected result. This is what "done" looks like.

## Open Questions

- [Unresolved decision needing operator input.]
