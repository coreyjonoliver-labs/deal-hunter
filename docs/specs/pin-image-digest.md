# Spec: Pin the published image digest in deploy/

**Status:** Complete
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-15
**Phase:** Phase 1 — Release hygiene
**Depends on:** None

---

## Summary

Pin `deploy/`'s image references to the digest CI published for 0.2.0, and add
a test that keeps those references in step with the package version.

## Goal

As a self-hoster applying `deploy/` as written, I want it to run exactly the
image that was reviewed, so that a later build of `main` (which republishes the
same version tag) can't change what my cluster or Compose host runs.

## Background

`.github/workflows/build.yml` tags the image with the package version and
republishes that tag on every build of `main`, so `:0.2.0` moves. The security
audit of the deal-hunter rename (PR #1) asked for the shipped manifests to pin
a digest before third parties are pointed at them; at the time no image under
the new name existed. The first `main` build after the merge published
`ghcr.io/coreyjonoliver-labs/deal-hunter:0.2.0@sha256:adf697e701345c6b0bd28300f85fc3ebc2915ac20403d643c83fb80015bf8ae0`
(run 38751614506) with build provenance, and the package is public.

## Requirements

1. The kustomize base (`deploy/kubernetes/base/kustomization.yaml`) shall set
   the image's `newTag` to the package version and `digest` to the published
   digest.
2. The example overlay shall inherit the base's pin (no `images:` entry of its
   own) and carry a comment warning that an entry setting only `newTag` drops
   the digest. `base/deployment.yaml`'s literal image lines shall carry the
   same tag and digest, so applying the raw manifest is pinned too.
3. The Compose file shall reference `ghcr.io/coreyjonoliver-labs/deal-hunter:<version>@sha256:<digest>`.
4. A test shall fail if any of those references names a version newer than
   `pyproject.toml`'s or isn't a release version, lacks a `sha256:` digest of
   64 hex characters, or if they don't all name the same tag and digest. Pins
   may lag the package version: a bump must merge (and `main` build it)
   before its digest exists, and the CI `image` job only runs after tests
   pass, so requiring equality would block every release.
5. The deploy docs shall say how to find the digest for a release (the build
   log's `pushed …` line, or the GHCR package page) and how to verify it
   (`gh attestation verify oci://… --repo coreyjonoliver-labs/deal-hunter`
   with `--cert-identity` set to the `build.yml@refs/heads/main` workflow,
   `--source-ref refs/heads/main` and `--deny-self-hosted-runners`, since
   `--repo` alone accepts any workflow on any branch), and warn that an
   overlay `images:` entry must set both `newTag` and `digest`.

## Acceptance Criteria

```gherkin
Given the deploy manifests as committed
When the test suite runs
Then every image reference names version 0.2.0 and the same sha256 digest

Given someone bumps pyproject.toml's version without updating deploy/
When the test suite runs
Then it passes, so the bump can merge and main can publish its image

Given deploy/ pins a version newer than pyproject.toml's, or one file's tag or
digest differs from the others
When the test suite runs
Then the test fails and names the offending file

Given the example overlay
When it's built with kustomize
Then the Deployment's three containers use image@sha256:<digest>
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `deploy/kubernetes/base/kustomization.yaml` | edit | `images:` gets `digest:` |
| `deploy/kubernetes/example/kustomization.yaml` | edit | drop `images:` (inherit the base pin); warning comment |
| `deploy/kubernetes/base/deployment.yaml` | edit | literal image lines get the tag + digest |
| `deploy/compose/compose.yaml` | edit | `image:` with `@sha256:` |
| `deploy/README.md` | edit | finding and verifying a release digest |
| `tests/test_deploy.py` | create | parses the four files line by line (no YAML dependency) |

## Data / State Impact

None.

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | `tests/test_deploy.py`; kustomize build in the gate |
| Coverage stays at or above the project's threshold | N/A | no threshold configured |
| Failure paths and edge cases are tested | Yes | mismatched version / missing digest cases |
| No secrets in code, config, tests, or fixtures | Yes | a digest is public |
| External input is validated before use | N/A | |
| No injection vector | N/A | |
| Errors fail closed and don't leak internals | N/A | |
| New dependencies justified and pinned | Yes | none added |
| Public interfaces documented; docs updated | Yes | `deploy/README.md` |
| Repo stays free of personal deployment details | Yes | only the public image name |

## Edge Cases

1. The release process later changes to immutable tags → the test still holds
   (tag + digest both pinned).
2. A future version bump: the bump merges with the old pins (the test allows
   pins to lag), `main` publishes the new image, then a follow-up change pins
   its verified digest — the procedure is in `deploy/README.md` and the
   conventions skill.

## Testing Strategy

- **Unit:** `tests/test_deploy.py` — consistency across files and with
  `pyproject.toml`.
- **Integration:** `kustomize build deploy/kubernetes/example | kubeconform`;
  `docker compose config` from a scratch copy.
- **Manual:** `gh attestation verify` against the pinned digest.

## Out of Scope

- Making release tags immutable in CI (a workflow change; operator-only — see
  BACKLOG `immutable-release-tags`).

## End-to-End Verification

```sh
uv run pytest -q tests/test_deploy.py
kustomize build deploy/kubernetes/example | grep 'image:'   # all @sha256:adf697e7…
gh attestation verify oci://ghcr.io/coreyjonoliver-labs/deal-hunter@sha256:adf697e701345c6b0bd28300f85fc3ebc2915ac20403d643c83fb80015bf8ae0 \
  --repo coreyjonoliver-labs/deal-hunter
```

## Open Questions

- None.
