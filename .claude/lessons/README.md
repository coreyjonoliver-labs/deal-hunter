# `.claude/lessons/` — the compounding (learning) store

This directory holds the **raw, immutable** side of the compounding loop (Phase 3 of
the workflow). It starts empty; the workflow fills it as you work.

- **`log.md`** — the **append-only, immutable raw log** of captured lessons. The review
  gate auto-appends each material finding (with an importance score and provenance), and
  `/learn` appends manual ones. **This file is never edited in place and never loaded
  into the agent's context** — it is the source of truth a retrospect rebuilds from.
- **`archive.md`** — lessons demoted out of the curated set during a retrospect (kept,
  not deleted), plus retired rules (see below).

The **curated, loaded** side lives elsewhere: `/retrospect` consolidates `log.md` into a
capped, ranked **`.claude/rules/lessons.md`** — a native rules file that loads
automatically. `/retrospect` always rebuilds that file *from `log.md`* (never from the
previous curated file), which is the rule that keeps consolidation from compounding its
own errors.

## Log entry shape

Each entry in `log.md` follows this shape (all fields after `finding` are optional but
encouraged — they make the log queryable and recurrence-aware at capture time, when
context is freshest):

```yaml
- date: <YYYY-MM-DD>
  source: "<PR/commit/file, or 'manual'>"
  importance: <1-10>
  mechanizable: <yes|no|unknown>
  scope: unclassified              # set at /retrospect: repo-neutral | repo-specific
  category: <taxonomy value>       # optional — see taxonomy below
  module: "<path or area>"         # optional — the code area this lesson is about
  tags: [<term>, ...]              # optional free-form list of key terms
  recurrence_of: "<date> / <short finding ref>"  # optional — see recurrence below
  finding: "<the distilled lesson + any precondition>"
```

Entries without the optional fields (pre-existing or hand-written) are always valid; they
are treated as `uncategorized` at `/retrospect` time. No migration of old logs is needed.

## Category taxonomy

Assign one category from this fixed list. Use `uncategorized` when nothing fits:

`build-test` · `runtime-errors` · `security` · `error-handling` · `performance` ·
`conventions` · `architecture` · `process`

A category outside this list (from hand-editing) is treated as `uncategorized` by
`/retrospect` with a note — never an error.

## Recurrence links (`recurrence_of`)

When `/learn` or the review gate captures a lesson that covers the same ground as a
prior entry (same category/module + overlapping key terms), the **new** entry carries a
`recurrence_of` reference pointing at the prior entry:

```yaml
recurrence_of: "2026-05-12 / handlers swallow errors"
```

The reference is an advisory link — never a hard foreign key. Two invariants hold without
exception:

- **The log is append-only.** No prior entry is ever edited, merged, or removed because
  of a match. Duplication in the log is signal, not waste — the log is never loaded.
- **Uncertain match → no link.** A missed link costs a little clustering quality; a wrong
  merge would cost an entry. When confidence is low, capture without a `recurrence_of`.

At `/retrospect` time, recurrence chains are treated as pre-clustered groups and the
occurrence count adds weight to the rule's ranking (a lesson logged three times outranks
an equal-importance one-off). A `recurrence_of` pointing at a missing or malformed entry
is treated as unlinked — the pass never fails on it.

## Retirement convention (`archive.md`)

`archive.md` holds two kinds of entries:

1. **Rank overflow** — lessons that were valid but fell below the top-N cap. These can
   re-promote on the next `/retrospect` if their relative ranking changes.
2. **Retired rules** — rules that the staleness pass in `/retrospect` determined are no
   longer live. Retired rules are appended to `archive.md` with a dated reason:

```
retired: 2026-06-05 — referent gone (src/legacy/ removed)
retired: 2026-06-05 — enforced by check-no-placeholders.sh
retired: 2026-06-05 — superseded by CLAUDE.md decision (operator confirmed)
```

Retirement is **non-permanent**: because `/retrospect` always regenerates the curated
set from the raw log (not from `archive.md`), a retired rule can re-promote if new log
entries re-establish it. The raw log `log.md` is **never touched** by retirement.

## The loop

```
work → review gate finds things → log.md (auto)        # capture, liberal + immutable
                  /learn "<lesson>"  → log.md (manual)
session-start nudges when enough has accumulated
→ /retrospect  → rebuilds .claude/rules/lessons.md from log.md
             → staleness pass retires stale/mechanized rules to archive.md
             → demotes overflow to archive.md
             → routes mechanizable lessons to hooks/tests, neutral ones to:
→ /contribute-lesson → a gated PR upstream into the spec-workflow plugin
```

Nothing here is secret-bearing — `/learn` refuses to record content that looks like a
credential. See the spec-workflow plugin's `docs/using-the-workflow.md` for the day-to-day flow.
