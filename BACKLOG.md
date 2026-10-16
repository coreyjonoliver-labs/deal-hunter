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
- [x] release-0-3-0 — publish 0.3.0 (Discord alerts, search signal, rating
  summaries): version bump → main publishes + attests → strict verify → pin
  the digest in `deploy/` in a follow-up
- [x] graduate-established — server-side merge boundary: required checks
  (test, image, secret-scan, dependency-review, codeql), branch protection,
  CODEOWNERS, sandbox `failIfUnavailable` (operator applies settings,
  workflow and protection)
- [x] ops-metrics — `/metrics` on its own port (heartbeat, search failures,
  session expiry, AI spend, notifications) + generic ServiceMonitor and alert
  rules in `deploy/` (picked up 2026-10-16, operator: ops-metrics then
  otel-tracing)
- [ ] otel-tracing — OTLP traces per search cycle with an attribute allowlist,
  off by default; builds on ops-metrics' notification/search events

## Up Next

- [ ] promtool-in-ci — `tests/test_deploy.py::test_prometheus_rules_pass_their_unit_tests`
  skips without promtool, so the alert-rule unit tests (`rules.test.yaml`) never
  run in CI; install promtool in `build.yml` (workflow change: operator-only)
- [ ] conventions-ops-metrics — add to the conventions skill: the `metrics`
  module; `notification` events from `launch.py` patch 10; the metrics port's
  IP-literal/allowed-host rule; `send_message_with_retry` and
  `send_email_message` in the bump-aimm patch list (skill dir: operator-only)
- [ ] search-budget-warning — every search phrase is one Marketplace page
  load per interval across ALL watches, and Facebook throttles (and may flag)
  busy logins, but nothing adds that cost up for the operator: a second watch
  with a preset can double the page loads silently. Compute the budget at
  startup and in `deal-hunter check` — Σ phrases × (1 / interval) = page
  loads per hour — print it, warn above a documented soft threshold (a default
  derived from what the live runs have tolerated, tunable in `[search]`),
  refuse to start above a hard ceiling unless `[search] allow_busy = true`,
  show it on the dashboard's settings panel, and export it as an ops-metrics
  gauge so an alert rule can watch it. Also warn when a settings change will
  trigger a re-rate pass (one AI call per cached listing of a changed watch)
  and say how many listings that is.
- [ ] bump-aimm — ai-marketplace-monitor 0.10.2 → latest. Upstream fixes the
  unconditional "Failed to get search results" and adds login recovery /
  search-radius handling, but changes every function deal-hunter
  patches (login, search, goto_url, create_page, launch_browser, and since
  then get_listings, AIResponse.to_cache, the ai-module rating `re.match`,
  NotificationConfig.notify_all); re-verify each patch-point test and the
  saved-session flow, then a live run.
  **Deferred (operator, 2026-10-15): not before** the newest aimm release is
  at least 5 days old with no newer release in that window (per PyPI,
  0.10.3–0.10.16 shipped Oct 5–14; re-check around Oct 22).
- [ ] immutable-release-tags — CI republishes the version tag on every `main`
  build; publish per-commit tags or refuse an existing version; gate the
  `image`/`attest` jobs on `refs/heads/main` (`workflow_dispatch` on a branch
  can publish and attest today; so can any pushed branch that edits
  `build.yml`'s trigger, so the fix must live outside the workflow — an
  environment-scoped publish credential limited to `main`, or registry-side
  immutable tags) (workflow change: operator-only). Until then
  the README's `docker run` examples pull by tag only (`:0.3.0`), so a paste
  gets the latest `main` build; show `:<version>@sha256:<digest>` with a
  pointer to the verify step in `deploy/README.md`
- [ ] verify-pins-in-ci — a CI step that runs the strict
  `gh attestation verify` on the digest pinned in `deploy/`, so a pinning PR
  can't merge an unattested or branch-built digest (workflow change:
  operator-only)
- [ ] scoped-agent-token — agent sessions run `gh` unsandboxed with the
  operator's admin token, which can change branch protection, dispatch
  workflows and merge; `permissions.ask` prompts are only a speed bump. Give
  agent sessions a fine-grained `GH_TOKEN` (this repo; contents + pull
  requests write; no Administration, Actions or Workflows write) so those are
  out of reach whatever the command (operator: token + env setup)
- [ ] chromium-sandbox-seccomp — Research: a `Localhost` seccomp profile that
  lets Chromium's sandbox start under runc, so `browser_sandbox = true` works in
  containers without a microVM

## Later

- [ ] watch-editor-ui — Research/decision first: let the dashboard add or
  edit `[[watch]]` tables while the TOML file stays the source of truth.
  Scrutiny so far, to be confirmed by the spec:
  - The dashboard is deliberately read-only (`deal-hunter web` only reads
    aimm's directory and the settings; it has no write path), it has no login
    of its own (a reverse-proxy gate in front of it is the control in every
    documented deployment), and it runs beside the browser that renders
    untrusted seller pages. A write path from the UI into the settings turns
    a dashboard compromise, or a cross-site request from an unrelated tab,
    into control of what the browser searches for and what the AI is told.
    Any editor with an "apply" therefore needs same-origin + CSRF tokens, a
    separate port or process from the read-only dashboard, and must never
    write the file the `run` process reads unless the deployment mounts it
    writable.
  - In container deployments the settings file is usually a read-only mount
    owned by the orchestrator (compose, a ConfigMap managed from git): the
    "apply" half has nowhere to write, and a writable overlay on the data
    volume would split the source of truth and silently survive a change the
    operator makes in the real file.
  - What works everywhere: a **watch composer** — a form that validates a
    watch (the `name` rule, prices, phrases or preset, keywords), shows the
    search-budget impact (page loads per hour before/after, see
    search-budget-warning) and the re-rate cost, previews what the first
    search would return (`deal-hunter check`-style), and emits the exact
    `[[watch]]` TOML to paste into the settings file. No write path, no new
    attack surface, works with git-managed and compose deployments alike.
  - Optional second step, only where the file is writable: an "apply" button
    gated by `[web] allow_settings_write = true` AND a writable settings path,
    writing an atomic replacement with a `.bak`, then signalling `run` to
    re-render (today: a restart). Off by default; documented as widening the
    dashboard from read-only to a config-writing surface, with its own threat
    model.
  Recommendation: build the composer (cheap, safe); treat "apply" as a
  separate spec.
- [ ] rating-parse-followups — the one-per-run "rating line didn't match"
  warning can fire on a reasoning line that quotes a listing's mid-line
  "Rating 5" (then reads "scored as unrated" wrongly and uses up its one
  shot); warn only on lines that *start* like a rating. Fail closed on
  off-scale shapes `Rating 5 / 10` and `Rating 1-5`. Residual (accepted): a
  later line that itself starts with "Rating n:" still wins (aimm's loop is
  last-wins)
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
