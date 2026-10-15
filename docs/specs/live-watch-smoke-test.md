# Spec: Live smoke test of a non-mini-PC watch

**Status:** Complete
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-15
**Phase:** Phase 1 — Release hygiene
**Depends on:** None

---

## Summary

Run deal-hunter 0.2.0 end to end against Facebook Marketplace with a generic
`[[watch]]` (a standing desk) next to the mini-PC watch, and record what
happened, so the generalization is proven on real listings and not only in
tests.

## Goal

As the maintainer, I want one recorded live run of a multi-watch settings file,
so that the generic prompt, per-watch filters and dashboard are known to work on
real listings before more watches or presets build on them.

## Background

0.2.0 (PR #1) replaced the hard-coded mini-PC search with `[[watch]]` tables
and a generic AI prompt. Everything was verified offline: rendered configs load
in aimm's own `Config`, filters run through aimm's own matcher, the dashboard
is tested against a synthetic cache. Nothing has searched Facebook or called
the AI with the generic prompt yet.

The run needs a logged-in Facebook session and an AI API key, which only the
operator holds, so the operator runs it; the agent prepares the steps and
records the results. No product code changes unless the run finds a bug, in
which case the bug gets its own spec.

## Requirements

1. The operator's legacy settings shall be converted to a `[[watch]]` file
   with the mini-PC watch named `nuc` (so cached ratings still apply) plus a
   generic `standing desk` watch, without the location entering the repo or
   this conversation.
2. One monitor run shall complete at least one full round of searches for
   every watch's search phrases.
3. The AI shall rate at least one standing-desk listing using the generic
   prompt, ending in a `Rating <n>: <summary>` line that names the item, its
   condition and an estimated fair price.
4. The dashboard shall show the run: results tagged with their watch, the
   `?watch=` filter working, AI usage counted, and no unexpected errors under
   "Recent problems".
5. The events file shall contain no secret values (API key, notifier tokens).
6. Results (counts, a few rated listings with titles generalized, any
   problems) shall be recorded in this spec and in `PROGRESS.md`; any bug
   found becomes a BACKLOG item.

## Acceptance Criteria

```gherkin
Given the converted settings with watches nuc and "standing desk"
When the operator runs `deal-hunter run` until each watch has searched once
Then the dashboard's summary shows searches > 0 and AI calls > 0
And at least one result is tagged "standing desk" with a 1–5 rating and a summary
And "Recent problems" has no errors other than ones explained in the results

Given the run's events file
When it is searched for the API key and notifier token values
Then there are no matches
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `docs/specs/live-watch-smoke-test.md` | edit | the Results section below |
| `PROGRESS.md`, `BACKLOG.md` | edit | record the run; file any bugs |

No product code changes. The settings converter is a throwaway script outside
the repo.

## Data / State Impact

| Entity / Store | Change | Details |
|---|---|---|
| `~/.ai-marketplace-monitor` (operator's machine) | update | aimm's cache and the events file gain the run's listings and ratings; the old `nuc-hunter-events.jsonl` is renamed by `run` |

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | N/A | a manual, live verification |
| Coverage stays at or above the project's threshold | N/A | no code change |
| Failure paths and edge cases are tested | Partly | the run's "Recent problems" are recorded |
| No secrets in code, config, tests, or fixtures | Yes | credentials stay in the operator's environment; requirement 5 checks the events file |
| External input is validated before use | N/A | |
| No injection vector | N/A | |
| Errors fail closed and don't leak internals | Yes | checked via the dashboard's problems list |
| New dependencies justified and pinned | N/A | |
| Public interfaces documented; docs updated | Yes | results recorded |
| Repo stays free of personal deployment details | Yes | no location, towns or listing URLs recorded; titles generalized |

## Edge Cases

1. Facebook asks for a checkpoint or the session expired → `run` says so; run
   `deal-hunter login` and retry. Recorded as a problem only if the tool
   handled it badly.
2. No desk listings in range → widen `radius` or `max_price` for the run and
   note it.

## Testing Strategy

- **Manual:** the runbook in the operator's session; the dashboard JSON
  summary; a `grep -c` of secret values in the events file.

## Out of Scope

- Fixing anything the run finds (each bug gets its own spec).
- Running the container or Kubernetes deployment.

## End-to-End Verification

The operator runs the converter, `deal-hunter check`, `deal-hunter run` until
the first round of searches and ratings, then `deal-hunter web` and the summary
query, and pastes the outputs; the results are recorded below.

## Results

Run on 2026-10-15 on the operator's desktop: deal-hunter 0.2.0, aimm 0.10.2,
`claude-haiku-4-5`, settings converted from the legacy format into watches
`nuc` (preset `mini-pc`) and `standing desk` (no preset, keywords `desk`).

| Requirement | Result |
|---|---|
| 1. Converted settings, location kept out of the repo and conversation | Pass. `check` drops a desk listing for `nuc` and passes it for `standing desk`. |
| 2. Every phrase searched at least once | **Partial.** Both watches searched (116 listings fetched, 37 updated in the last 24 h), but aimm logged "Failed to get search results" for 4 of the 6 phrases (`minisforum`, `mini pc`, `standing desk`, `sit stand desk`) and one "Failed to parse search results … list index out of range". → BACKLOG `investigate-search-failures`. |
| 3. Generic prompt rates a desk listing | Pass on judgment, **partial on format.** 15 AI calls, ~$0.05 total. Desk ratings were sensible: a rolltop and an art/drafting desk rated 1 as the wrong product, a 32" adjustable desk rated 1 against the 55" minimum, a cheap VIVO standing desk and a desk with hutch rated 2 with questions for the seller. But no summary named an estimated fair price, and some ended in a stray `**` (markdown bold leaking into the comment). → BACKLOG `rating-summary-format`. |
| 4. Dashboard | Pass. `?watch=standing desk` showed only desk results, each tagged with the watch; AI usage counted (15 calls). 87 listings filtered out before the AI; 0 matches at rating 3+. |
| 5. No secrets in the events file | Pass. The API key's value occurs 0 times in `deal-hunter-events.jsonl`. |

Also noticed: aimm's "Failed to get search results for <phrase> from <city>"
message carries the Marketplace location id into the events file and the
dashboard's problems list. That's fine for the operator's own dashboard, but
it's location data in a place that's easy to screenshot or paste; noted in
`investigate-search-failures`.

## Open Questions

- None.
