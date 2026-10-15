# Backlog

The work queue. `/next-task` takes the first unchecked item in **Current Focus**;
when that's empty it promotes from **Up Next**. Keep items small enough to be one
spec each.

## Current Focus

- [x] pin-image-digest — pin the published 0.2.0 digest in `deploy/` and test
  that it stays in step with the package version
- [x] live-watch-smoke-test — run a non-mini-PC `[[watch]]` against Facebook end
  to end (operator-run: needs a saved session and an API key)
- [x] investigate-search-failures — the live run logged "Failed to get search
  results" for 4 of 6 phrases and a "list index out of range" parse error in
  aimm 0.10.2; find out whether it's Facebook markup, throttling or the
  session, and fix or patch it in `launch.py` (also: that message puts the
  Marketplace location id into the events file / dashboard)
- [x] rating-summary-format — rating summaries omit the estimated fair price
  the prompt asks for, and some end in a stray `**` (markdown leaking into the
  comment); tighten the prompts and/or clean the comment before display and
  notification
- [x] discord-deal-alerts — post matching listings to a Discord webhook as
  cards (photo, price, rating, AI summary, watch); spec Ready. Alerting is
  scoped to "a product you want just got listed" (operator decision
  2026-10-15)

- [ ] bump-aimm — ai-marketplace-monitor 0.10.2 → 0.10.16: fixes the
  unconditional "Failed to get search results" upstream and adds login
  recovery / search-radius handling, but changes every function deal-hunter
  patches (login, search, goto_url, create_page, launch_browser, and since
  then get_listings, AIResponse.to_cache, the ai-module rating `re.match`,
  NotificationConfig.notify_all); re-verify each patch-point test and the
  saved-session flow, then a live run

## Up Next

- [ ] graduate-established — `/graduate` hardening for the established profile:
  branch protection requiring CI on `main`, CODEOWNERS, sandbox
  `failIfUnavailable` (operator applies settings/protection)
- [ ] immutable-release-tags — CI republishes the version tag on every `main`
  build; publish per-commit tags or refuse an existing version; gate the
  `image`/`attest` jobs on `refs/heads/main` (`workflow_dispatch` on a branch
  can publish and attest today) (workflow change: operator-only)
- [ ] verify-pins-in-ci — a CI step that runs the strict
  `gh attestation verify` on the digest pinned in `deploy/`, so a pinning PR
  can't merge an unattested or branch-built digest (workflow change:
  operator-only)
- [ ] chromium-sandbox-seccomp — Research: a `Localhost` seccomp profile that
  lets Chromium's sandbox start under runc, so `browser_sandbox = true` works in
  containers without a microVM

## Later

- [ ] rating-parse-followups — the one-per-run "rating line didn't match"
  warning can fire on a reasoning line that quotes a listing's mid-line
  "Rating 5" (then reads "scored as unrated" wrongly and uses up its one
  shot); warn only on lines that *start* like a rating. Fail closed on
  off-scale shapes `Rating 5 / 10` and `Rating 1-5`. Residual (accepted): a
  later line that itself starts with "Rating n:" still wins (aimm's loop is
  last-wins)
- [ ] ops-metrics — `/metrics` on its own port (heartbeat, search failures,
  session expiry, AI spend, notifications) + generic ServiceMonitor and alert
  rules in `deploy/`; spec Ready, deferred: alerting is scoped to new listings
  for now (a stalled monitor shows only on the dashboard's status card)
- [ ] otel-tracing — OTLP traces per search cycle with an attribute allowlist,
  off by default (depends on ops-metrics); spec Ready, deferred with it
- [ ] config-hardening — unknown keys rejected in every settings table (not
  just `[[watch]]`), a `schema_version` with migrations (legacy `[search]` as
  the first), `deal-hunter config check`, a published JSON Schema; move
  `facebook.session_file` to a flag/env (it's process wiring, not intent)
- [ ] dashboard-own-pod — run `web` in its own pod so it's a separate trust
  domain from the monitor (needs the cache shared without SQLite WAL across
  kernels)

<!--
Conventions:
- One line per task; the slug should match a spec name in docs/specs/.
- [ ] = not done, [x] = done. Tick only after the review gate passes.
- Note dependencies inline, e.g. "(depends on data-model)".
- Don't fabricate a long roadmap up front; capture what's genuinely queued.
-->
