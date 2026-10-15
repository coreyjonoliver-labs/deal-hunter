# Spec: OpenTelemetry tracing

**Status:** Ready
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-15
**Phase:** Phase 3 — Alerting & observability
**Depends on:** ops-metrics

---

## Summary

Emit OpenTelemetry traces for each search cycle (search phrase → page loads →
listing details → AI rating → notification), exported over OTLP to any
collector, off unless configured, with a strict allowlist of span attributes so
no listing text, location or secret ever leaves in telemetry.

## Goal

As an operator, I want to see where a search cycle spends its time and where it
fails (a slow Facebook page, AI latency, a retried navigation, a failed
notification), so that problems like the smoke test's failed searches can be
diagnosed from one trace instead of reading aimm's log.

## Background

`ops-metrics` says *that* searches fail and how often; it can't say *why* or
*where*. The live smoke test logged "Failed to get search results" for 4 of 6
phrases with no timing or step information. A trace per cycle shows each
phrase's page loads (with retries), each listing fetch, each AI call and each
notification as spans with durations and outcomes.

deal-hunter wraps aimm at known points in `launch.py` (navigation with timeout
and retries, login/search with the saved session, the browser launch, and —
after `discord-deal-alerts` and `ops-metrics` — notify and search outcomes).
Those patch points are where spans go; aimm itself isn't modified, and no
auto-instrumentation is used: auto-instrumenting `requests`/`httpx` would add
`traceparent` headers to calls to Facebook and the AI provider, leaking trace
ids to third parties, and would record URLs.

Traces leave the process, so what they carry matters more than for local logs.
Seller text is untrusted and personal (titles, descriptions, towns), the
Marketplace location is the operator's, and URLs carry listing ids and tracking
parameters. The safe design is an allowlist enforced in code, not a convention.

The OpenTelemetry Python SDK reads standard `OTEL_*` environment variables
(`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_TRACES_EXPORTER`, `OTEL_TRACES_SAMPLER`,
`OTEL_SDK_DISABLED`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`), so a
desktop, Compose or Kubernetes user configures it the usual way. Metrics stay on
the pull `/metrics` endpoint from `ops-metrics`; OTel is used for traces only.

## Requirements

1. Tracing shall be off unless `OTEL_EXPORTER_OTLP_ENDPOINT` or
   `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set, and `OTEL_SDK_DISABLED=true`
   shall turn it off regardless. When off, no OTel SDK objects are created and
   behavior is unchanged.
2. When on, the monitor (`run`) shall export spans over OTLP/HTTP (protobuf),
   with resource `service.name=deal-hunter` (overridable by `OTEL_SERVICE_NAME`)
   and `service.version` set to the package version, using a batch span
   processor.
3. Spans:
   - `deal_hunter.cycle` (root): one search round of all watches.
   - `deal_hunter.search` per watch and phrase, child of the cycle.
   - `deal_hunter.navigate` per page load, with retry count and outcome.
   - `deal_hunter.listing` per listing-detail fetch.
   - `deal_hunter.ai_rate` per AI call, with provider, model, token counts,
     outcome and the rating (1–5) — not the prompt or response text.
   - `deal_hunter.notify` per notification method attempt, with method and
     outcome.
   - `deal_hunter.session_save` when the session is saved.
4. Failures shall set span status `ERROR` with an exception *type* attribute
   (`exception.type`) and a fixed, deal-hunter-authored description; exception
   messages and stack traces from aimm, Playwright or HTTP clients shall not be
   recorded (they can contain URLs, page text or the location).
5. Span attributes shall be limited to an allowlist enforced by a span
   processor that removes everything else before export: `deal_hunter.watch`
   (validated watch name), `deal_hunter.phrase_index`, `deal_hunter.attempt`,
   `deal_hunter.outcome`, `deal_hunter.count.*` (integers), `deal_hunter.rating`,
   `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
   `gen_ai.usage.output_tokens`, `http.response.status_code`,
   `notify.method`, `exception.type`. In particular, no search phrase text,
   location, seller town, listing title/description/id/URL, image URL, cookie,
   header, prompt, AI response, API key or webhook URL.
6. No trace context shall be propagated on outbound requests (no `traceparent`
   or `baggage` headers to Facebook, the AI provider or notifiers): the global
   propagator shall be a no-op and no HTTP auto-instrumentation installed.
7. Before `run` restarts itself (`os.execv`) or exits, pending spans shall be
   flushed with a bounded timeout (≤ 5 s); an unreachable collector shall never
   block or crash the monitor (export errors are logged once per minute at
   most, without the endpoint's credentials if any).
8. The OTel packages shall be an optional extra `tracing`
   (`[project.optional-dependencies]`: `opentelemetry-api`,
   `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`), locked in
   `uv.lock` and installed in the container image; on a desktop install without them, setting the
   endpoint shall print one warning and run untraced.
9. Docs: `README.md` (how to turn it on, what's recorded and what never is) and
   `deploy/README.md` (the env vars for Compose and Kubernetes, the collector
   host to add to the egress allowlist, an optional kustomize component that
   sets them).

## Acceptance Criteria

```gherkin
Given no OTEL_* variables are set
When `deal-hunter run` starts
Then no OpenTelemetry tracer provider is created and the run is unchanged

Given OTEL_EXPORTER_OTLP_ENDPOINT points at an in-memory/fake OTLP collector
When one search cycle runs (aimm faked as in the launch tests)
Then the collector receives a deal_hunter.cycle trace with search, navigate, ai_rate and notify children carrying the allowlisted attributes

Given a span on which code sets "listing.title", "url.full" and an exception message containing the location
When it is exported
Then the exported span has none of them (allowlist processor)

Given an outbound request made during a traced cycle
When it is sent
Then it carries no traceparent or baggage header

Given the collector is unreachable
When a cycle ends and when run restarts via exec
Then the monitor continues, the flush gives up within 5 s, and at most one export error per minute is logged

Given OTEL_SDK_DISABLED=true and an endpoint set
When run starts
Then tracing stays off
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `src/deal_hunter/tracing.py` | create | setup from env, allowlist span processor, no-op propagator, flush helper |
| `src/deal_hunter/launch.py` | edit | spans at the existing patch points |
| `src/deal_hunter/cli.py` | edit | init before `run`; flush before `_restart` / exit |
| `pyproject.toml`, `uv.lock` | edit | optional `tracing` extra |
| `Dockerfile` | edit | install the `tracing` extra |
| `tests/test_tracing.py` | create | in-memory exporter, allowlist, propagation, off-by-default, flush |
| `deploy/kubernetes/components/tracing/` | create | optional component setting the `OTEL_*` env on `monitor` |
| `README.md`, `deploy/README.md` | edit | enabling, what's recorded, egress |

## Data / State Impact

None in deal-hunter's own state. Spans go to the operator's collector.

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | `tests/test_tracing.py` with an in-memory exporter and a fake OTLP server |
| Coverage stays at or above the project's threshold | N/A | no threshold |
| Failure paths and edge cases are tested | Yes | unreachable collector, missing packages, disabled SDK, exec restart |
| No secrets in code, config, tests, or fixtures | Yes | allowlist processor; test asserts env secrets never appear in exported spans |
| External input is validated before use | Yes | only validated watch names and integers become attributes |
| No injection vector | N/A | spans aren't interpreted |
| Errors fail closed and don't leak internals | Yes | exception messages dropped; export failure never stops the monitor |
| New dependencies justified and pinned | Yes | OTel API/SDK/OTLP-HTTP exporter, optional extra, locked |
| Public interfaces documented; docs updated | Yes | README, deploy README |
| No trace context sent to third parties | Yes | no-op propagator; test on outbound headers |
| Repo stays free of personal deployment details | Yes | example collector host is generic |

## Edge Cases

1. Sampling: honour `OTEL_TRACES_SAMPLER`; default parent-based always-on (a
   cycle per hour is low volume).
2. The browser's own network traffic is not traced (Chromium isn't
   instrumented); page loads are spans around Playwright calls.
3. `web` is not traced in this spec (read-only, low value).

## Testing Strategy

- **Unit:** allowlist processor (every forbidden attribute dropped), setup
  on/off matrix, flush timeout.
- **Integration:** a faked aimm cycle through the real patch points with an
  in-memory exporter; a local fake OTLP/HTTP endpoint for the exporter path;
  outbound-header check.
- **Manual:** point a desktop run at a local collector (e.g. Grafana Alloy or
  the OTel Collector with a debug exporter) and inspect one trace.

## Out of Scope

- A trace backend (e.g. Grafana Tempo, Jaeger) and the collector that receives
  OTLP: deployment-specific, set up outside this repo.
- Metrics or logs over OTLP (metrics stay pull-based; logs stay in the events
  file).
- Tracing `web`, or instrumenting aimm internals beyond the patch points.
- Trace ids in the events file / dashboard (possible later).

## End-to-End Verification

```sh
uv sync --locked --extra tracing
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 uv run deal-hunter -s deal-hunter.toml run
# a local collector with a debug exporter prints one deal_hunter.cycle trace
# per round, children per phrase/listing/AI call, only allowlisted attributes
uv run pytest -q tests/test_tracing.py
```

## Open Questions

- Should listing ids (numeric Marketplace ids) be allowed as an attribute to
  correlate a slow `deal_hunter.listing` span with the dashboard? They're not
  personal, but they identify a seller's listing; this spec leaves them out.
