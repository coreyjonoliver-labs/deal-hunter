# Spec: Rating summaries that name a fair price, in plain text

**Status:** Complete
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-15
**Phase:** Phase 1 — Release hygiene
**Depends on:** None

---

## Summary

Make every AI rating summary a short plain-text line that always states a fair
price (or says it's unknown), and clean the stray markdown that leaks into
summaries today, so the dashboard and alerts show something you can act on.

## Goal

As a buyer, I want each rating's one-line summary to tell me what it is, what
shape it's in, what it's worth versus the asking price, and whether to buy,
ask or skip, without formatting debris, so that an alert is enough to decide.

## Background

The live smoke test (`docs/specs/live-watch-smoke-test.md`) found two problems
in the summaries aimm stores and shows:

1. **No fair price.** Both rating prompts (`profile.GENERIC_RATING_PROMPT`,
   `profile.MINI_PC_RATING_PROMPT`) ask for a summary that names the item, its
   condition, an estimated fair price and buy/ask/skip, but as one loose
   sentence. None of the five desk summaries named a price.
2. **Stray markdown.** Some summaries ended in `**`. aimm 0.10.2 parses the
   answer in `OpenAIBackend.evaluate` and `AnthropicBackend.evaluate` with
   `re.match(r".*Rating[^1-5]*([1-5])[:\s]*(.*)", line)`: the summary is the
   rest of the rating line, plus any lines after it. A model that bolds its
   conclusion (`**Rating 2: …**`) leaves the closing `**` in the summary.

Both backends call `res.to_cache(listing, item_config, marketplace_config)` on
the `AIResponse` before returning it, so wrapping `AIResponse.to_cache` (the
established `launch.py` patch pattern) can clean the summary once, for the
cache, the notifiers and the dashboard alike. Summaries already in the cache
from earlier runs are cleaned at display time.

Changing a prompt changes aimm's item-config hash, which is part of its rating
cache key. Listings are therefore **re-rated the next time a search returns
them** — a one-time AI cost (the smoke test's rate was 15 ratings for about
$0.05).

## Requirements

1. Both rating prompts shall require the final line to be exactly
   `Rating <1-5>: <item>; <condition>; fair ~$<amount> vs $<asking> ask; <buy|ask|skip>`
   — plain text, no markdown, nothing after it — with `fair unknown` in place
   of the price when the model can't estimate one, and one worked example
   line. The mini-PC prompt's `<item>` names model, CPU and RAM/storage. The
   line shall still match aimm's parse (`Rating[^1-5]*[1-5]`).
2. deal-hunter shall clean every rating summary before aimm caches it, by
   wrapping `AIResponse.to_cache`. Cleaning:
   - removes markdown emphasis and code markers (`**`, `__`, `*`, `_` when
     used as emphasis delimiters, backticks) and leading `#`/`>` markers;
   - removes a duplicated leading `Rating <n>:`;
   - collapses whitespace;
   - truncates to 300 characters on a word boundary with `…`.

   It leaves words, numbers, `$`, `~`, `;` and `/` intact.
3. The dashboard shall apply the same cleaning to summaries read from the cache,
   so ratings stored before this change display cleanly. The JSON API returns
   the cleaned text.
4. A patch-point test shall pin that both pinned backends' `evaluate` call
   `to_cache` on the response object before returning it, and that
   `AIResponse` keeps a `comment` field, so an aimm bump that moves the cache
   write fails loudly.
5. `README.md`: the summary format with an example, and that changing prompts
   (by upgrading deal-hunter or editing a watch) re-rates listings as they're
   seen again.

## Acceptance Criteria

```gherkin
Given a model answer whose last line is "**Rating 4: Uplift V2 60x30; light wear; fair ~$350 vs $220 ask; buy**"
When aimm parses it through the real evaluate path (AI client faked)
Then the cached and returned summary is "Uplift V2 60x30; light wear; fair ~$350 vs $220 ask; buy"

Given a cached rating with the comment "Rolltop desk, wrong product category—skip.**"
When the dashboard shows it
Then it shows "Rolltop desk, wrong product category—skip."

Given the generic and mini-PC rating prompts
When they are rendered into the aimm config
Then each contains the exact final-line template, "fair unknown", and an example that aimm's rating regex parses with the intended score

Given a summary of 400 characters
When it is cleaned
Then it is at most 300 characters, ends on a word boundary with "…"

Given the installed aimm source
When the patch-point test runs
Then it finds res.to_cache(...) before return in both evaluate methods
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `src/deal_hunter/profile.py` | edit | both rating prompts |
| `src/deal_hunter/summary.py` | create | `clean_summary(text) -> str` |
| `src/deal_hunter/launch.py` | edit | wrap `AIResponse.to_cache`; docstring list of fixes |
| `src/deal_hunter/dashboard.py` | edit | clean comments at display |
| `tests/test_summary.py` | create | cleaning table: markdown variants, duplicate prefix, truncation, untouched text |
| `tests/test_launch.py` | edit | patch-point test; real evaluate path with a faked client |
| `tests/test_filters.py` | edit | prompt template and example parse checks (the existing aimm-format tests live here) |
| `tests/test_dashboard.py` | edit | old cached comment cleaned on display |
| `README.md` | edit | summary format; re-rating note |

## Data / State Impact

| Entity / Store | Change | Details |
|---|---|---|
| aimm `ai-inquiries` cache | new entries | prompt change → new item-config hash → listings re-rated when seen again; old entries stay (and display cleaned) |

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | as listed |
| Coverage stays at or above the project's threshold | N/A | no threshold |
| Failure paths and edge cases are tested | Yes | empty summary, only markdown, very long, nested emphasis, `$`/`~` preserved |
| No secrets in code, config, tests, or fixtures | Yes | none involved |
| External input is validated before use | Yes | summaries are AI output shaped by seller text: cleaned, length-capped, still HTML-escaped on the dashboard |
| No injection vector | Yes | cleaning only removes characters; output escaping unchanged |
| Errors fail closed and don't leak internals | Yes | a cleaning error leaves aimm's summary unchanged rather than dropping the rating (logged once) |
| New dependencies justified and pinned | Yes | none |
| Public interfaces documented; docs updated | Yes | README |
| aimm patch has a patch-point test | Yes | requirement 4 |
| Prompts keep the untrusted-text guard | Yes | the evaluation prompts are unchanged; only the rating prompts change |

## Edge Cases

1. The model writes extra lines after the rating line anyway → aimm appends
   them; cleaning collapses them and the 300-character cap bounds them.
2. The model can't price it → `fair unknown` is the expected, valid output.
3. A summary that is entirely markdown → cleaned to empty → keep aimm's
   original rather than an empty summary.

## Testing Strategy

- **Unit:** the cleaning table; prompt contents; the example line through
  aimm's own regex.
- **Integration:** aimm's real `evaluate` (both backends) with a faked client
  returning fixed answers, through the patched `to_cache`.
- **Manual:** one live run; record how many new summaries contain `fair ~$` or
  `fair unknown` (target: all), and that none end in markdown.

## Out of Scope

- Structured (JSON) AI output or tool calling (would need deeper aimm changes).
- Re-rating the existing cache proactively.
- Discord card formatting (`discord-deal-alerts`, which will reuse the cleaner).

## End-to-End Verification

```sh
uv run pytest -q tests/test_summary.py tests/test_launch.py tests/test_dashboard.py tests/test_filters.py
uv run deal-hunter -s ~/deal-hunter-smoke.toml run      # one round, Ctrl-C
curl -s http://127.0.0.1:8740/api/summary.json | jq -r '.results[] | select(.score != null) | .comment' | head
```

## Open Questions

- None.
