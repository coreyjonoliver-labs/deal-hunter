# Spec: Operational metrics for Prometheus

**Status:** Complete
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-15
**Phase:** Phase 3 — Alerting & observability
**Depends on:** None

---

## Summary

Expose deal-hunter's health as Prometheus metrics (whether it's still
searching, search failures, Facebook session expiry, AI usage and spend,
notifications), and ship generic Kubernetes scrape and alert-rule examples in
`deploy/`, so any Prometheus/Alertmanager setup can alert when the monitor
stops doing its job.

## Goal

As an operator, I want an alert when deal-hunter stops finding listings (stalled
searches, failing searches, an expired Facebook login, runaway AI spend), not
only when its pod is down, so that problems like the smoke test's failed
searches surface without me opening the dashboard.

## Background

What a cluster already knows about deal-hunter is liveness: kube-state-metrics
(restarts, OOM kills, readiness), cAdvisor (CPU/memory) and, in a typical
setup, a blackbox probe of `/healthz`. None of it says whether the monitor is
working. In the live smoke test (`docs/specs/live-watch-smoke-test.md`) the pod
was running and `/healthz` was green while aimm failed to get search results
for 4 of 6 phrases.

deal-hunter already has the data: `web` builds a `Snapshot` from aimm's cache
and the events file (`events.py`) — searches, AI usage and estimated cost
(`dashboard.PRICES`), problems, status (running / starting / stalled), and the
session's saved/expiry metadata. This spec turns that into a `/metrics`
endpoint, plus a few structured events the monitor doesn't record yet (per-watch
search failures, notifications).

Metrics are served by `web`, not the monitor: `web` already reads everything
read-only, survives monitor restarts (the monitor restarts by `exec`, which
would reset in-process counters), and runs in its own container. The dashboard
port has no login and checks the `Host` header, which a scraper sends as the pod
IP; metrics therefore get their own port, opt-in, serving nothing but
`/metrics`, so a NetworkPolicy can admit the scraper to metrics without
admitting it to the dashboard.

Naming follows a convention meant to be reusable by any periodic job: a
`<app>_last_success_timestamp_seconds` heartbeat gauge, so one generic
"heartbeat stale" rule can cover many services.

## Requirements

1. `deal-hunter web` shall accept `--metrics-port PORT` (default: off) and
   `--metrics-host HOST` (default: the `--host` value). When set, it shall serve
   `GET /metrics` on that port in Prometheus text exposition format 0.0.4, and
   404 for every other path, with no dashboard content. The `Host` header must
   be an IP literal (a scraper uses the pod IP; IPv6 bracketed, port optional)
   or one of the dashboard's allowed host names, else 403, so a DNS-rebound
   name can't reach the port from a browser.
2. The metrics endpoint shall expose, with the `deal_hunter_` prefix:
   - `deal_hunter_last_success_timestamp_seconds` (gauge): the end of the most
     recent search round in which at least one phrase of any watch returned
     results; absent before the first one.
   - `deal_hunter_watch_last_success_timestamp_seconds{watch}` (gauge): the same,
     per watch.
   - `deal_hunter_searches_total{watch,outcome="ok"|"failed"}` (counter): phrase
     searches by outcome, built on the `search_results` events
     (`investigate-search-failures`): count > 0 is ok, 0 is failed.
   - `deal_hunter_listings{state="fetched"|"rated"|"filtered_out"|"matched"}`
     (gauge): the dashboard's counts.
   - `deal_hunter_ai_calls_total{model}`, `deal_hunter_ai_tokens_total{model,kind="input"|"output"|"cache_read"|"cache_write"}`,
     `deal_hunter_ai_cost_dollars_total{model}` (counters; cost estimated at list
     prices, absent for models without a price).
   - `deal_hunter_notifications_total{method,outcome="sent"|"failed"}` (counter).
   - `deal_hunter_problems_total{kind="error"|"warning"}` (counter).
   - `deal_hunter_session_present` (gauge 0/1),
     `deal_hunter_session_saved_timestamp_seconds`,
     `deal_hunter_session_cookie_expiry_timestamp_seconds` (gauges, absent when
     unknown). None of the session series are exported when
     `facebook.anonymous` is set, so session rules can't fire for an
     anonymous monitor.
   - `deal_hunter_search_interval_seconds{bound="min"|"max"}` (gauge, from
     settings), so a stall rule can use the configured interval.
   - `deal_hunter_build_info{version,aimm_version}` (gauge, always 1).
   - `deal_hunter_cache_read_errors` (gauge): problems reading the cache for this
     scrape, plus 1 when the last snapshot refresh failed and the scrape is
     served from the previous snapshot (0 when clean). While it is not 0 from
     a cache read error, the `deal_hunter_listings` series are omitted. The
     refresh failure's exception class is printed once per outage to stderr.
3. Counters shall be derived from the events file, so they reset when the file
   is trimmed (`events.MAX_BYTES`); this shall be documented, and `rate()` /
   `increase()` handle it as a counter reset.
4. Label values shall be limited to: configured watch names (validated by settings: 1–40 characters, so bounded by the number of watches), model
   names, and the fixed enumerations above. No metric or label shall contain a
   search phrase, the Marketplace location, seller towns, listing titles, URLs,
   AI text, or any secret.
5. The monitor shall record structured events the metrics need and don't exist
   yet. Search outcomes are the `search_results` events that
   `investigate-search-failures` adds (watch, phrase index, count; never the
   phrase text or the location); aimm's "Failed to get search results" message
   is logged after every search and is not a failure signal. This spec adds
   `notification` (method, outcome) for every notify attempt. Raw aimm
   messages recorded as problems shall have the Marketplace location value
   replaced with `[location]`.
6. Computing `/metrics` shall reuse the dashboard's `SnapshotCache` (no
   second read of the cache per scrape), and shall answer within 2 s on a
   cache of 20,000 listings.
7. `deploy/kubernetes/components/prometheus/` shall add, as an optional
   kustomize component: the `--metrics-port 9741` arg on `web`, a `metrics`
   port on the Service, a NetworkPolicy ingress rule on 9741 from namespaces
   labelled `deal-hunter/metrics-access: "true"`, a `ServiceMonitor`, and a
   `PrometheusRule` with:
   - `DealHunterSearchStalled` (critical): no successful search for longer
     than the max interval plus 30 minutes, guarded by an `absent()` leg for
     the heartbeat metric.
   - `DealHunterSessionExpiringSoon` (warning): cookie expiry within 3 days.
   - `DealHunterSessionExpired` (critical): cookie expiry in the past, or
     `deal_hunter_session_present == 0` while not anonymous.
   - `DealHunterSearchFailures` (warning): more than 50% of phrase searches
     failed over 1 h, with at least 4 searches.
   - `DealHunterAISpendHigh` (warning): `increase(deal_hunter_ai_cost_dollars_total[24h])`
     above a threshold set in one place (default $1.00), commented as the knob.
   Rules shall carry no secret-bearing or free-text labels, and shall ship with
   `promtool test rules` unit tests.
8. Docs: `README.md` (the flag, the metric list, the counter-reset note) and
   `deploy/README.md` (the component, the port, which namespaces may scrape,
   and the alert rules).

## Acceptance Criteria

```gherkin
Given `deal-hunter web --metrics-port 9741`
When Prometheus scrapes http://<pod-ip>:9741/metrics with Host <pod-ip>:9741
Then it gets 200 with text format 0.0.4 containing deal_hunter_build_info
And GET http://<pod-ip>:9741/ returns 404 and no dashboard HTML

Given an events file with a search round where 2 of 6 phrases failed
When /metrics is scraped
Then deal_hunter_searches_total{outcome="failed"} sums to 2 and {outcome="ok"} to 4
And deal_hunter_last_success_timestamp_seconds equals that round's end

Given a search_results event with count 0 for a watch and phrase index
When /metrics is scraped
Then deal_hunter_searches_total{outcome="failed"} counts it, with no phrase text or location

Given a session whose login cookie expires in 2 days
When the rules are evaluated (promtool test)
Then DealHunterSessionExpiringSoon fires and DealHunterSessionExpired doesn't

Given no deal_hunter_last_success_timestamp_seconds series at all
When the rules are evaluated (promtool test)
Then DealHunterSearchStalled fires (absent guard)

Given the whole /metrics output
When it is checked against the watch names, phrases, location and env secrets of the test settings
Then none of the phrases, the location or any secret appear
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `src/deal_hunter/metrics.py` | create | Snapshot + events → exposition text; no third-party dependency |
| `src/deal_hunter/dashboard.py` | edit | metrics listener (own server, `/metrics` only), shares `SnapshotCache` |
| `src/deal_hunter/events.py` | edit | `notification` events (search outcomes come from `search_results`) |
| `src/deal_hunter/launch.py` | edit | where aimm's search/notify outcomes are observed (patch points with tests) |
| `src/deal_hunter/cli.py` | edit | `web --metrics-port/--metrics-host` |
| `tests/test_metrics.py` | create | exposition, values, label hygiene; parsed with a text-format parser in tests |
| `deploy/kubernetes/components/prometheus/` | create | kustomization, ServiceMonitor, PrometheusRule, rule tests |
| `tests/test_deploy.py` | edit | the component renders; rule tests run when `promtool` is on PATH |
| `README.md`, `deploy/README.md` | edit | flag, metrics, rules, component |

## Data / State Impact

| Entity / Store | Change | Details |
|---|---|---|
| events file (`deal-hunter-events.jsonl`) | update | new event kinds; problem messages have the location redacted |

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | `tests/test_metrics.py`, `tests/test_events.py`, promtool rule tests |
| Coverage stays at or above the project's threshold | N/A | no threshold |
| Failure paths and edge cases are tested | Yes | empty home, unreadable cache, trimmed events, unknown model |
| No secrets in code, config, tests, or fixtures | Yes | label-hygiene test over the full output |
| External input is validated before use | Yes | events file treated as untrusted (existing read caps); watch names validated in settings |
| No injection vector | Yes | label values escaped per the exposition format; only enumerated/validated values |
| Errors fail closed and don't leak internals | Yes | a broken cache yields metrics without the cache-derived series plus the `cache_read_errors` gauge, never a stack trace |
| New dependencies justified and pinned | Yes | none at runtime; a text-format parser in tests only if needed (dev group, locked) |
| Public interfaces documented; docs updated | Yes | README, deploy README |
| Metrics port exposes no dashboard content | Yes | `/metrics` only, 404 otherwise; Host must be an IP or an allowed name; separate NetworkPolicy rule |
| Repo stays free of personal deployment details | Yes | generic namespace label, example threshold |

## Edge Cases

1. `web` started before the monitor ever ran → heartbeat absent; the `absent()`
   leg makes the stall rule fire after its `for:` window, not stay silent.
2. Events file trimmed → counters drop; `increase()` treats it as a reset.
3. Old events from before this spec (no `search_results`) → search
   counters start at the first new event; the heartbeat falls back to
   `search_summary` events.
4. Forged or aborted events: an event stamped more than 300 s after the
   scrape's clock is ignored (for the heartbeat and the dashboard status), and
   a monitor restart (`browser_ready`) abandons the round in progress.
5. Anonymous mode → no session series at all, so the session rules (which
   key on those series) stay quiet; the stall rule still covers a monitor that
   Facebook blocks.

## Testing Strategy

- **Unit:** metrics text from fixed Snapshots/events; label hygiene; event
  recording and location redaction.
- **Integration:** a real `web` with `--metrics-port` on an ephemeral port,
  scraped over HTTP; the dashboard port still enforces `Host`.
- **Rules:** `promtool test rules` fixtures for every alert (fires / doesn't).
- **Manual:** scrape a real run's `web`; check the series against the dashboard.

## Out of Scope

- Wiring a specific cluster (its Prometheus egress rules, namespace labels,
  where its rule files live) — done in that deployment's own config.
- A shared, cross-service "heartbeat stale" rule — belongs to whichever
  monitoring setup adopts the `<app>_last_success_timestamp_seconds`
  convention.
- Grafana dashboards (a later spec; the metrics are designed to support one).
- Tracing (`otel-tracing`) and Discord deal alerts (`discord-deal-alerts`).
- Pushing metrics (OTLP or Pushgateway): pull only.

## End-to-End Verification

```sh
uv run deal-hunter -s deal-hunter.toml web --metrics-port 9741 &
curl -s http://127.0.0.1:9741/metrics | grep -E '^deal_hunter_(last_success|searches_total|session_cookie)'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9741/        # 404
promtool test rules deploy/kubernetes/components/prometheus/rules.test.yaml
kustomize build deploy/kubernetes/example   # with the component added: ServiceMonitor + PrometheusRule render
```

## Open Questions

- Metrics port number: 9741 proposed (next to the dashboard's 8740). Change it
  before implementation if it collides with an existing scrape port.
