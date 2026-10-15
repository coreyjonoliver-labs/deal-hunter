# Spec: Truthful search-failure signal

**Status:** Complete
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-15
**Phase:** Phase 1 — Release hygiene
**Depends on:** None

---

## Summary

Stop recording aimm 0.10.2's false "Failed to get search results" error (it is
logged after every search), record what each phrase search actually returned,
and keep the Marketplace location out of recorded messages.

## Goal

As an operator, I want "search failed" on the dashboard (and later in metrics
and alerts) to mean a search really came back empty, so that a real outage
stands out instead of drowning in an error logged after every search.

## Background

The live smoke test (`docs/specs/live-watch-smoke-test.md`) showed "Failed to
get search results for <phrase> from <location>" for 4 of 6 phrases. The
investigation found it is not a failure signal at all. In aimm 0.10.2,
`FacebookMarketplace.search` does:

```python
found_listings = FacebookSearchResultPage(self.page, ...).get_listings()
time.sleep(5)
if self.logger:
    self.logger.error(f"... Failed to get search results for {search_phrase} from {city}")
```

with no `if not found_listings` — the error follows **every** search. The
operator's aimm log confirms it: 14 searches started, 13 such errors (the 14th
was in progress); in the rotated log, 186 and 185. Upstream fixed it later:
0.10.16 logs a *warning* "No search results for …" only when a search returns
nothing (and adds login recovery and search-radius handling around it).

The second message, "[Retrieve] Failed to parse search results N listing: list
index out of range", is real but small: 23 times in ~200 searches aimm skipped
one result tile whose markup didn't match (`details_divs[1]`); 0.10.16 still has
the same code. It stays recorded as aimm reports it.

The false error also carries the Marketplace location (aimm's `city`, the
`search_city` value) into the events file and the dashboard's "Recent problems",
which puts the operator's location into recorded messages (the events file and the dashboard's problems list).

Upgrading aimm to 0.10.16 would fix the false error upstream but changes the
functions deal-hunter patches (login, search, navigation, page creation,
browser launch); that's its own spec (BACKLOG `bump-aimm`). This spec fixes the
signal in deal-hunter against the pinned 0.10.2, and lays down the per-phrase
events `ops-metrics` needs (its search counters would otherwise count every
search as failed).

## Requirements

1. The events recorder shall not record aimm's "[Search] Failed to get search
   results for … from …" message (as an error, a warning or anything else).
   A patch-point test shall assert the pinned aimm still logs it
   unconditionally after `get_listings()`, so a version bump forces this rule
   to be revisited.
2. For every phrase search, deal-hunter shall record a `search_results` event
   with the watch (aimm item name), the phrase's 0-based index within that
   watch, and the number of listings `get_listings()` returned — never the
   phrase text or the location. When the count is 0, it shall also record a
   `warning` problem: "No search results for watch '<watch>' phrase #<n>".
3. Every message the events recorder writes (problems, and any other free
   text) shall have the configured Marketplace location value replaced with
   `[location]`, in addition to the existing secret redaction. The value comes
   from the rendered aimm config the monitor was started with (`--config`),
   so `launch.main`'s signature doesn't change.
4. The dashboard's status and problems shall reflect the new events: no
   "Failed to get search results" lines; "No search results" warnings when
   searches really come back empty.
5. aimm's "[Retrieve] Failed to parse search results N listing" error shall
   continue to be recorded as aimm reports it (location redaction applies).
6. `README.md`'s dashboard section shall say what "No search results" means;
   the conventions skill's "Bump ai-marketplace-monitor" procedure shall list
   this patch among the ones to re-check.

## Acceptance Criteria

```gherkin
Given the pinned aimm and a faked search page returning 3 listings for phrase 0 and 0 for phrase 1
When one search of a watch with two phrases runs through the real aimm search loop
Then the events file holds search_results {watch, phrase_index: 0, count: 3} and {phrase_index: 1, count: 0}
And one warning "No search results for watch '<watch>' phrase #1"
And no event contains "Failed to get search results"

Given search_city "springfieldtest" in the rendered config
When aimm logs any message containing "springfieldtest"
Then the recorded message contains "[location]" and not "springfieldtest"

Given aimm logs "[Retrieve] Failed to parse search results 2 listing: list index out of range"
When it is recorded
Then it appears as an error problem, unchanged apart from redaction

Given the installed aimm source
When the patch-point test runs
Then it finds the unconditional "Failed to get search results" log after get_listings() (and fails if a bump removed it)
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `src/deal_hunter/launch.py` | edit | wrap `FacebookSearchResultPage.get_listings` to count per phrase; know the current watch from `search_with_session`; read `search_city` from the `--config` file for redaction |
| `src/deal_hunter/events.py` | edit | drop the false message; `search_results` events and "No search results" warnings; extra redaction values |
| `tests/test_launch.py` | edit | patch-point tests (unconditional log still there; `get_listings` signature); faked search loop |
| `tests/test_events.py` | edit | drop/redact/record behavior |
| `tests/test_dashboard.py` | edit | problems list from the new events |
| `README.md`, `.claude/skills/deal-hunter-conventions/SKILL.md` | edit | dashboard meaning; bump procedure |
| `docs/specs/ops-metrics.md` | edit | build its search counters on `search_results` (count > 0 → ok, 0 → failed) |

## Data / State Impact

| Entity / Store | Change | Details |
|---|---|---|
| events file | update | new `search_results` kind; location redacted in messages from now on (older lines unchanged) |

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | as listed |
| Coverage stays at or above the project's threshold | N/A | no threshold |
| Failure paths and edge cases are tested | Yes | zero results, missing `--config`, unreadable config (redaction falls back to none, logged once) |
| No secrets in code, config, tests, or fixtures | Yes | fixtures use a fake city slug |
| External input is validated before use | Yes | counts are ints; watch names come from the validated config |
| No injection vector | N/A | |
| Errors fail closed and don't leak internals | Yes | location redaction on every recorded message |
| New dependencies justified and pinned | Yes | none |
| Public interfaces documented; docs updated | Yes | README, conventions skill |
| aimm patch has a patch-point test | Yes | `get_listings` signature + the unconditional log |
| Repo stays free of personal deployment details | Yes | the investigation's numbers carry no location |

## Edge Cases

1. A search page that fails to parse entirely (aimm saves the page and returns
   `[]`) → count 0 → "No search results" warning, plus aimm's own parse error.
2. Pagination or a second `get_listings()` call in one phrase search (not in
   0.10.2) → the phrase index comes from the search loop, not a call counter
   alone; the patch-point test pins one call per phrase.
3. The rendered config lacks `search_city` (shouldn't happen) → no location
   redaction beyond the existing rules; one warning logged.

## Testing Strategy

- **Unit:** event recording, dropping, redaction.
- **Integration:** aimm's real `FacebookMarketplace.search` with a fake page /
  `FacebookSearchResultPage` (as the existing launch tests fake Playwright).
- **Manual:** a live run: no "Failed to get search results" in the dashboard;
  `search_results` events present with counts.

## Out of Scope

- Upgrading aimm (BACKLOG `bump-aimm`).
- Fixing aimm's tile-parsing gap (`details_divs[1]`).
- Metrics and alerts on these events (`ops-metrics`).

## End-to-End Verification

```sh
uv run pytest -q tests/test_launch.py tests/test_events.py tests/test_dashboard.py
uv run deal-hunter -s deal-hunter.toml run      # live: one round, then Ctrl-C
grep -c 'Failed to get search results' ~/.ai-marketplace-monitor/deal-hunter-events.jsonl   # no new lines
grep '"search_results"' ~/.ai-marketplace-monitor/deal-hunter-events.jsonl | tail -3
```

## Open Questions

- None.
