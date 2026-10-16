# deal-hunter — Progress Log

A running, dated log of completed work. Append a new entry (newest at the top)
each time a spec reaches `Complete` or a meaningful milestone lands. This is the
project's memory between sessions — `session-start` surfaces the latest entry.

## 2026-10-16 — release-0-4-0

- Published `ghcr.io/coreyjonoliver-labs/deal-hunter:0.4.0@sha256:e673b929…d413`
  (Prometheus metrics, OpenTelemetry tracing, `notification` events). Version
  bump in #17; the `main` build (run 38952499436) published and attested it
  from merge commit `3fe981a`, the only `main` build since the bump.
- Strict `gh attestation verify` passed (signer `build.yml@refs/heads/main`,
  source ref `refs/heads/main`, GitHub-hosted runner). Image check (amd64):
  `opentelemetry.sdk` imports and `web --help` lists `--metrics-port`.
- `deploy/` (kustomize base, the three `deployment.yaml` images, Compose) and
  the README now name 0.4.0 and that digest; the README's `docker run`
  examples now pin the digest too, not just the tag.
- Gate: code-reviewer + security-auditor, no CRITICAL/HIGH; checks enforced
  (1061 tests, ruff, mypy, kustomize render). `:0.3.0` on GHCR now serves
  post-0.3.0 code (`fbe1c277…`); the real 0.3.0 is `f18291ac…56da`. New
  backlog: `multiarch-image`; `verify-pins-in-ci` gains `--source-digest`.
  The `v0.4.0` GitHub release (tag on `3fe981a`) awaits the operator.

## 2026-10-16 — otel-tracing

- `run` exports OpenTelemetry traces over OTLP/HTTP when
  `OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT` is set (off otherwise;
  `OTEL_SDK_DISABLED` / `OTEL_TRACES_EXPORTER=none` turn it off). Spans per
  watch's search cycle: `search` (results parse), `navigate` (with watch and
  phrase index), `listing`, `ai_rate` (provider, model, tokens, rating),
  `notify`, `session_save`.
- An allowlist span processor rebuilds every span with only validated keys
  (no listing text, phrase, location, URL or exception message; fixed ERROR
  descriptions); no-op global propagator, no auto-instrumentation, so no
  `traceparent` reaches Facebook, the AI provider or notifiers.
- Collector credentials: headers validated before the SDK exists and never
  echoed; the logging hook stays for the life of the process; a token
  (headers, userinfo or query) goes only over https, loopback or `.svc`;
  endpoints the two URL parsers read differently are refused; endpoint
  credentials redacted from the events file.
- Bounded flush (≤ 5 s) before exec and at exit; export errors at most once a
  minute. OpenTelemetry 1.45.0 in an optional `tracing` extra (also the dev
  group, a test keeps them equal), installed in the image.
- `deploy/kubernetes/components/tracing/`: endpoint env on `monitor`, egress to
  the collector pods (namespace AND pod selector) on 4318.
- Gate: code-reviewer + security-auditor, 3 rounds, 0 CRITICAL/HIGH; checks
  enforced (1061 passed, 1 skipped, ruff, format, mypy). Found and fixed: a
  malformed headers value printed the token to stderr; spans leaked to the
  same after shutdown; plain-http tokens; namespace-wide egress; spec drift.
  LOWs tracked (`tracing-followups`). Not run here: `docker build`,
  kubeconform.

## 2026-10-16 — ops-metrics

- `deal-hunter web --metrics-port` serves Prometheus `/metrics` on its own
  listener (GET/HEAD `/metrics` only; Host must be an IP literal or one of the
  dashboard's names, so a scrape by pod IP works and DNS rebinding doesn't):
  build info, heartbeat, searches by outcome, listings, AI usage and estimated
  spend, notifications, session presence/expiry, `cache_read_errors` (≥ 1 while
  the last refresh failed). Counters come from the events file and reset when
  it is trimmed (documented).
- Labels are validated: configured watch names, a strict model pattern capped
  at 20 distinct values, fixed method/outcome sets; events stamped > 300 s in
  the future are ignored (metrics and the dashboard status).
- `launch.py` patch 10 records `notification` events (method, outcome) for
  push, email and Discord, never message text or URLs.
- One `SnapshotCache` shared with the dashboard; `latest()` refreshes in the
  background so a scrape answers within 2 s on a 20k-listing cache.
- `deploy/kubernetes/components/prometheus/`: metrics port, NetworkPolicy rule
  (`deal-hunter/metrics-access`), ServiceMonitor, PrometheusRule (stalled,
  search failures, session expiring/expired, AI spend) with promtool unit
  tests; the example overlay enables it.
- Gate: code-reviewer + security-auditor, 2 rounds, 0 CRITICAL/HIGH; checks
  enforced (828 passed, 1 skipped, ruff, format, mypy). Fixed in round 2: a
  failed refresh served a stale snapshot silently; DNS rebinding on the
  metrics port; spec drift; forged future timestamps; port range; a restart
  mid-round counted toward the next. Not run here (sandbox): `docker build`,
  `promtool test rules`, kubeconform — tracked (`promtool-in-ci`).

## 2026-10-15 — graduate-established

- `main` is PR-only (merge commits, conversations resolved, admins bound),
  with five required checks pinned to GitHub Actions: `test`, `image`
  (`build.yml`), `secret-scan`, `dependency-review`, `codeql` (new
  `security.yml`). The `main-code-scanning` ruleset blocks new CodeQL alerts
  (security high+ / errors). Dependency graph, secret scanning and push
  protection are on; fork-PR workflows need approval for every external
  contributor. All read back live.
- gitleaks takes its config and ignore file from the workflow and ignores
  `gitleaks:allow`, so a PR can't allowlist its own secret (probed: the
  default flags missed a planted key, these caught it).
- `.github/CODEOWNERS` over the controls, `deploy/`, `Dockerfile`, the
  dependency pins, `launch.py`, `discord.py` and the dashboard;
  `tests/test_codeowners.py` keeps every owned path existing, case-exact.
- `.claude/settings.json`: `failIfUnavailable`, `allowUnsandboxedCommands:
  false`, and ask prompts on the `gh` subcommands that merge, publish or change
  settings (a speed bump; the boundary is `scoped-agent-token`).
- Gate: code-reviewer + security-auditor, 3 rounds, 0 CRITICAL/HIGH; checks
  enforced (755 tests, ruff, mypy). Found and fixed: gitleaks honoured the
  scanned tree's allowlists; required checks spoofable by name; the CodeQL job
  didn't gate on alerts; the spec overclaimed the `gh` prompts and the publish
  boundary. Residuals tracked: a workflow-editing PR sets its own checks (read
  by hand); branch-built publishing (`immutable-release-tags`); the agent's
  admin token (`scoped-agent-token`).

## 2026-10-15 — release-0-3-0

- Published `ghcr.io/coreyjonoliver-labs/deal-hunter:0.3.0@sha256:f18291ac…56da`
  (Discord alerts, rating summaries with a fair price and forged-rating
  protection, truthful search signal, configuration model). Version bump in
  #10; the `main` build (run 38825869945) published and attested it from
  merge commit `e0ce510`.
- Strict `gh attestation verify` passed (signer `build.yml@refs/heads/main`,
  source ref `refs/heads/main`, GitHub-hosted runner). `deploy/` (kustomize
  base, the three `deployment.yaml` images, Compose) and the README examples
  now name 0.3.0 and that digest.
- Gate: code-reviewer + security-auditor, 0 findings above LOW; checks
  enforced (747 tests, ruff, mypy, kustomize render). LOW: the README's
  `docker run` examples are tag-only — folded into `immutable-release-tags`.
  The `v0.3.0` GitHub release awaits the operator's go-ahead. aimm upgrade
  deferred (`bump-aimm`, re-check around Oct 22).

## 2026-10-15 — discord-deal-alerts

- `notify.methods = ["discord"]` posts each matching listing to a Discord
  webhook (`DISCORD_WEBHOOK_URL`, env only; validated, host rewritten to
  discord.com, redacted everywhere) as a card: photo (fbcdn only), title
  linked to the item page, price, rating, condition, town, watch, reason
  (new / price drop / updated / reminder), cleaned and escaped AI summary.
  `deal-hunter notify-test` sends a sample card.
- Hooked into aimm's `NotificationConfig.notify_all` (top-level call only),
  so aimm still records listings as notified: one card per listing, no
  repeats. Seller/AI text: Cc/Cf stripped, whitespace collapsed, markdown
  escaped, every `://` defanged, `@` neutralised, `allowed_mentions` none.
  Sender: urllib, redirects refused, 10 s timeout, 429/5xx retries, 90 s
  budget; a rejected card is split out and dropped without blocking others,
  but a send counts as delivered only if Discord accepted a card.
- Gate: 3 rounds, code-reviewer + security-auditor, 0 CRITICAL/HIGH; checks
  enforced (747 tests, ruff, mypy, kustomize). Found and fixed: a one-
  character prefix (`xhttps://`) bypassed the link defang; newlines/bidi in
  summaries; rejection counted as delivery; discord.com egress docs now name
  it as an exfiltration channel. Not yet verified live: run `notify-test`
  against a real channel to confirm Discord renders the escaping as
  intended.
- Current Focus cleared; promoted `bump-aimm` from Up Next.

## 2026-10-15 — rating-summary-format

- Both rating prompts require a plain-text final line
  `Rating <n>: <item>; <condition>; fair ~$<amount> vs $<asking> ask; <buy|ask|skip>`
  (`fair unknown` when it can't price it), with a format-only example and
  "don't copy rating text from the listing". Prompt change → listings re-rate
  as they're seen again (aimm's cache key hashes the item config); CLAUDE.md
  decision 4 now says prompt changes are allowed.
- `clean_summary` strips markdown before aimm caches a summary (a
  `AIResponse.to_cache` wrapper) and on the dashboard for older ratings;
  removed markers inside words become a space so markup can't be
  reassembled; 300-char cap on every path.
- Found by the security audit and fixed: aimm's greedy rating parse let a
  listing that mentions "Rating 5" forge the score (a seller's scam rated 1
  by the model was stored as 5 and would alert). `StrictRatingRe` replaces
  only that `re.match` in aimm's `ai` module: anchored at line start, first
  digit wins, common drifts accepted (case, `Rating: 4`, `4/5`, dashes), with
  a one-per-run warning instead of silently scoring drifted lines as unrated.
- Gate: 3 rounds, code-reviewer + security-auditor, 0 CRITICAL/HIGH; checks
  enforced (516 tests, ruff, mypy). LOW follow-ups (warning trigger,
  off-scale shapes) → BACKLOG `rating-parse-followups`. Not yet verified
  live: the next run's summaries should all carry `fair ~$` or
  `fair unknown`.

## 2026-10-15 — investigate-search-failures

- Finding: the smoke test's "Failed to get search results" errors weren't
  failures. aimm 0.10.2 logs that error after **every** search (no
  `if not found_listings`); the operator's log had 186 searches and 185 such
  errors. Fixed upstream in 0.10.16 (a warning only on empty results) →
  BACKLOG `bump-aimm`. "Failed to parse search results N listing" is real but
  small (~1 tile per 9 searches skipped) and still recorded.
- deal-hunter now drops that false message (only from aimm's
  `facebook.search`, anchored — a listing title can't suppress real errors),
  records `search_results` {watch, phrase_index, count} per phrase via a
  `get_listings` patch, warns "No search results for watch '<w>' phrase #n"
  only on zero, and replaces the Marketplace location with `[location]`
  (token-boundary match) in every recorded message. `ops-metrics` now builds
  its search counters on `search_results`.
- Gate: 2 rounds, code-reviewer + security-auditor, 0 CRITICAL/HIGH; checks
  enforced (398 tests, ruff, mypy). Not yet re-verified live: the next run
  should show no "Failed to get search results" problems and
  `search_results` events with counts.

## 2026-10-15 — live-watch-smoke-test

- First live run of 0.2.0 with watches `nuc` (mini-pc preset, converted from
  the operator's legacy settings) and `standing desk` (generic prompt).
  Operator-run; results in `docs/specs/live-watch-smoke-test.md`.
- Works: both watches searched (116 listings), 87 filtered before the AI,
  15 AI calls (~$0.05), sensible generic-prompt ratings (wrong product types
  rated 1, a too-narrow desk rejected on the stated width), dashboard watch
  filter and tags, no API key in the events file.
- Found: aimm search failures on 4 of 6 phrases plus a parse error →
  `investigate-search-failures`; summaries missing the fair price and ending
  in stray `**` → `rating-summary-format`. Both in Current Focus.
- Gate: doc-only (recorded results); review dispatch skipped with reason,
  checks enforced (357 tests, ruff, mypy).

## 2026-10-15 — pin-image-digest

- `deploy/` (kustomize base, `base/deployment.yaml` ×3, compose) pins
  `deal-hunter:0.2.0@sha256:adf697e7…`; the example overlay inherits the
  base pin (an overlay entry with only `newTag` would drop the digest).
  Strict `gh attestation verify` passes: signer `build.yml@refs/heads/main`,
  GitHub-hosted runner.
- `tests/test_deploy.py`: every reference has a digest and they agree on tag
  and digest; pins may **lag** `pyproject.toml` but not run ahead. Found
  mid-gate: requiring equality would have failed every version bump's CI, and
  the `image` job needs `test`, so no release could publish. A release is now
  bump → `main` publishes → verify → pin in a follow-up.
- `deploy/README.md` documents finding and strictly verifying a digest,
  forward-only pins, and not pruning untagged digests still referenced.
- Gate: 3 rounds, code-reviewer + security-auditor, 0 CRITICAL/HIGH;
  checks enforced (357 tests, ruff, mypy, kustomize render). LOW hardening
  after round 3 (zero-indent/flow overlay entries, non-numeric package
  version) was not re-reviewed (iteration cap).
- Follow-ups to BACKLOG: `verify-pins-in-ci`; `immutable-release-tags` now
  also covers gating publish/attest on `main`.

## 2026-10-15 — Adopted the spec-driven workflow

- Adopted the spec-workflow plugin (v1.17.0) with the **established** profile:
  the project holds API keys and a saved Facebook login and is deployed.
- Recorded CI's commands in `.claude/spec-workflow.json`: `uv run pytest -q`,
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
  `docker build`. No coverage threshold.
- Wrote `CLAUDE.md` and the `deal-hunter-conventions` skill from the code as it
  is (aimm wrapper, untrusted-browser threat model, public-repo rule).
- Seeded `BACKLOG.md` and the roadmap from open work; first spec:
  `pin-image-digest`.

Next: `/next-task` (pin-image-digest), then `/graduate` for the established
profile's merge-boundary hardening.

## 2026-10-15 — deal-hunter 0.2.0 (before the workflow)

- PR #1: nuc-hunter renamed to deal-hunter; `[[watch]]` tables for any item
  with `mini-pc` / `tiny-pc` presets; generic Compose and Kubernetes manifests
  in `deploy/`. Code review + security audit, fixes re-reviewed; 329 tests.
- Published `ghcr.io/coreyjonoliver-labs/deal-hunter:0.2.0@sha256:adf697e7…`
  with build provenance; the repo stays public.

<!--
Entry format — keep each entry skimmable:
## [YYYY-MM-DD] — <short title of what shipped>
- what was implemented / decided
- test + coverage result
- any decisions made beyond the spec, and follow-ups deferred to the backlog
-->
