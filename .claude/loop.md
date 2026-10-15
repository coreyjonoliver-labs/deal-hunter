# Loop body — tending scope

This file defines the default body for bare `/loop` in this project.
**Scope: tending work already in motion. Not initiating new work.**

## What to do each loop turn

Work through the following in order. If nothing applies, say so and stop.

### 1. Continue In Progress specs

For each spec in `docs/specs/` with `**Status:** In Progress`:

- Check whether the implementation is complete and the gate has run.
- If implementation is not yet started or is incomplete, continue it.
- If the implementation is done but the gate has not run, invoke `/review-gate
  <spec-name>` to run the full gate.
- Do not advance tracking (BACKLOG tick, PROGRESS entry, roadmap update) until
  the gate passes and the stamp exists.

### 2. Tend the current branch's open PR

For the current branch, if an open PR exists:

- **CI failures** — read the failing check output; fix the root cause in the
  code (not the check); push the fix.
- **Review comments** — address each unresolved comment; push fixes; re-request
  review only after every comment is addressed or has a clear response.
- **Merge conflicts** — rebase or merge the target branch; resolve conflicts;
  push the result.

Only push when continuing something already in progress on this branch. Do not
push to a protected branch; the guard chain denies that regardless.

### 3. Tracking-file hygiene

- Check for completed items in `BACKLOG.md` that are not ticked — tick them only
  if their gate stamp exists.
- Check for a `PROGRESS.md` entry that is missing for a recently completed item
  — add it (dated, one-paragraph summary of what was implemented).
- Check for `docs/roadmap.md` rows whose status has not been updated to match the
  spec's current Status — update them.

Do not promote items from `Up Next` to `Current Focus`, add new backlog items, or
create new specs. Those are initiatives — outside this scope.

---

## Scope guardrail (verbatim — must not be loosened)

This loop body is scoped to **tending, not initiating**:

- **No new initiatives outside this scope.** Do not create new specs, add items
  to `BACKLOG.md`, reprioritize the backlog, or start work on anything not
  already In Progress or explicitly authorized by the operator earlier in this
  session transcript.
- **Irreversible actions (push, delete) only when continuing something the
  transcript already authorized.** A push is irreversible; a delete is
  irreversible. Only take these actions when the current turn is a direct
  continuation of work the operator started or explicitly authorized in this
  session. When uncertain, stop and ask.

## Workflow invariant (restated in full — must hold on every turn)

Marking a spec `Complete`, ticking the backlog, or writing a `PROGRESS.md` entry
**before the gate has run and passed** is a process violation, not a shortcut.
The gate must run; the stamp must be written (`.claude/gate/<spec-name>.json`);
only then may `Status` be set to `Complete` and tracking updated.

The edit guard (`edit-write-guard.sh`) denies any edit that sets a spec's
`**Status:**` to `Complete` unless the gate stamp exists — so the invariant is
mechanized, not advisory. If the guard denies the Complete edit, the remedy is:
run `/review-gate <spec-name>` (which writes the stamp on pass). The guard fails
closed: a missing stamp, an unreadable gate directory, or any ambiguity about
the spec name all result in a deny, never a silent allow.

The only sanctioned skip is a sub-agent dispatch on a change with nothing in its
domain (a doc-only or test-only diff) — the gate itself always runs, and the skip
is stated with its reason. No silent skips.
