# Spec: Release 0.4.0

**Status:** Complete
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-16
**Phase:** Phase 1 — Release hygiene
**Depends on:** ops-metrics, otel-tracing

---

## Summary

Publish deal-hunter 0.4.0 (Prometheus metrics and OpenTelemetry tracing) as an
attested image, and pin `deploy/` to it.

## Goal

As a self-hoster, I want a versioned, verified image with `web --metrics-port`
and OTLP tracing, so that `deploy/` (and any downstream deployment) can turn on
the Prometheus component and alert rules, or point the monitor at a collector,
against a pinned digest.

## Background

Since 0.3.0 (`v0.3.0` = `e0ce510`), `main` has merged:

- #12, #13: the established-profile merge boundary (no image change).
- #14 (ops-metrics): `web --metrics-port` serving Prometheus `/metrics`;
  `notification` events; the `prometheus` kustomize component with a
  ServiceMonitor and five alert rules.
- #15 (otel-tracing): OTLP/HTTP traces from `run` behind an attribute
  allowlist, off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set; the `tracing`
  extra (OpenTelemetry 1.45.0) now installed in the image; the `tracing`
  kustomize component.
- #16: backlog only.

`pyproject.toml` still says `0.3.0`. So the `main` builds of #14 and #15
republished `:0.3.0` with this new code (BACKLOG `immutable-release-tags`),
while `deploy/` pins the original 0.3.0 digest `sha256:f18291ac…56da`.

The process is the same two steps as `release-0-3-0`:

1. The version bump merges first, because the image and its provenance only
   exist once `main` builds it. `tests/test_deploy.py` allows pins to lag the
   package version for this reason.
2. A follow-up pins the verified digest.

## Requirements

1. **Step 1 (bump):** `pyproject.toml`'s version shall be `0.4.0`, and
   `uv.lock` shall be updated to match. This change makes no other code change.
   `deploy/` keeps its 0.3.0 pins, and the test suite shall pass.
2. **Publish:** after step 1 merges, the `main` build shall publish
   `ghcr.io/coreyjonoliver-labs/deal-hunter:0.4.0` and attest it. The digest is
   read from that run's `pushed …@sha256:` line.
3. **Verify:** the digest shall pass the strict check from `deploy/README.md`
   before it is pinned.
4. **Image contents:** the published image shall contain the `tracing` extra,
   checked by importing `opentelemetry.sdk` in it. `deal-hunter web --help`
   shall list `--metrics-port`.
5. **Step 2 (pin):** these shall name `0.4.0` and the verified digest, and
   `tests/test_deploy.py` shall pass:
   - the kustomize base (`newTag` + `digest`);
   - the three image lines of `base/deployment.yaml`;
   - the Compose file;
   - the README's image references.
6. **GitHub release:** a release `v0.4.0` shall summarise the changes since
   0.3.0. Its tag goes on the version-bump merge commit. The operator creates
   it, or it is created with their explicit go-ahead, since it's public.

## Acceptance Criteria

```gherkin
Given step 1 merged to main
When the build workflow finishes
Then ghcr.io/coreyjonoliver-labs/deal-hunter:0.4.0@sha256:<d> exists and the strict attestation verify passes for <d>

Given the image <d>
When `python -c "import opentelemetry.sdk"` and `deal-hunter web --help` run in it
Then the import succeeds and the help lists --metrics-port

Given step 2
When the test suite runs
Then tests/test_deploy.py passes with every pin naming 0.4.0 and <d>
And kustomize build deploy/kubernetes/example renders all three containers as …:0.4.0@sha256:<d>
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `pyproject.toml`, `uv.lock` | edit | step 1: version 0.4.0 |
| `deploy/kubernetes/base/kustomization.yaml`, `deploy/kubernetes/base/deployment.yaml`, `deploy/compose/compose.yaml` | edit | step 2: tag + digest |
| `README.md` | edit | step 2: image examples |

## Data / State Impact

None in the repo. Deployments that move to 0.4.0 get:

- the new `notification` events in the events file;
- nothing else unless they opt in, via `--metrics-port` / the `prometheus`
  component, or an `OTEL_EXPORTER_OTLP_ENDPOINT`.

Prompts are unchanged, so nothing is re-rated.

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | `tests/test_deploy.py`; kustomize render; image smoke check |
| Coverage stays at or above the project's threshold | N/A | no threshold |
| Failure paths and edge cases are tested | Yes | the deploy test's existing failure cases |
| No secrets in code, config, tests, or fixtures | Yes | digests are public |
| External input is validated before use | N/A | |
| No injection vector | N/A | |
| Errors fail closed and don't leak internals | N/A | |
| New dependencies justified and pinned | Yes | none new in this change; OpenTelemetry 1.45.0 arrived with #15 |
| Public interfaces documented; docs updated | Yes | README image examples |
| Provenance verified before pinning | Yes | requirement 3 |

## Edge Cases

1. **Another commit lands on `main` between the bump and the pin.** Its build
   republishes `:0.4.0` with a new digest. Pin the digest of the build whose
   provenance you verified, and say which build in the PR.

## Testing Strategy

- **Unit:** `tests/test_deploy.py` (existing).
- **Manual:** the strict attestation verify; the image smoke check; the
  kustomize render.

## Out of Scope

- The aimm upgrade (`bump-aimm`).
- Immutable release tags (`immutable-release-tags`).
- Switching any specific deployment to 0.4.0.

## End-to-End Verification

```sh
gh attestation verify oci://ghcr.io/coreyjonoliver-labs/deal-hunter@sha256:<d> \
  --repo coreyjonoliver-labs/deal-hunter \
  --cert-identity https://github.com/coreyjonoliver-labs/deal-hunter/.github/workflows/build.yml@refs/heads/main \
  --source-ref refs/heads/main --deny-self-hosted-runners
docker run --rm --entrypoint python ghcr.io/coreyjonoliver-labs/deal-hunter@sha256:<d> -c "import opentelemetry.sdk"
docker run --rm ghcr.io/coreyjonoliver-labs/deal-hunter@sha256:<d> web --help | grep -- --metrics-port
uv run pytest -q tests/test_deploy.py
kustomize build deploy/kubernetes/example | grep 'image:'
```

## Open Questions

- None.
