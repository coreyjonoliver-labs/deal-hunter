# Spec: Release 0.3.0

**Status:** Complete
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-15
**Phase:** Phase 1 — Release hygiene
**Depends on:** None

---

## Summary

Publish deal-hunter 0.3.0 (Discord deal alerts, truthful search signal, rating
summaries with a fair price and forged-rating protection) as an attested image,
and pin `deploy/` to it.

## Goal

As a self-hoster, I want a versioned, verified image that contains everything
merged since 0.2.0, so that `deploy/` (and any downstream deployment) can move
to it deliberately instead of following a moving tag.

## Background

`main` has merged PRs #6 (search-failure signal), #7 (configuration model), #8
(rating summaries) and #9 (Discord alerts) since 0.2.0, but `pyproject.toml`
still says `0.2.0`, so every `main` build republishes `:0.2.0` with new code
(BACKLOG `immutable-release-tags`), while `deploy/` pins the original 0.2.0
digest `sha256:adf697e7…`.

A release takes two changes, as `deploy/README.md` "Pinning the image" and the
conventions skill describe: the version bump must merge first, because the
image and its provenance only exist once `main` builds it
(`tests/test_deploy.py` allows pins to lag the package version for exactly this
reason); then a follow-up pins the verified digest.

The aimm upgrade is deliberately not part of this release (BACKLOG `bump-aimm`:
deferred until upstream's rapid releases settle and meet the 5-day cooldown).

## Requirements

1. **Step 1 (bump):** `pyproject.toml`'s version shall be `0.3.0` (and
   `uv.lock` updated to match), with no other code change. `deploy/` keeps its
   0.2.0 pins in this change, and the test suite shall pass.
2. **Publish:** after step 1 merges, the `main` build shall publish
   `ghcr.io/coreyjonoliver-labs/deal-hunter:0.3.0` and attest it; the digest is
   read from that run's `pushed …@sha256:` line.
3. **Verify:** the digest shall pass the strict check from `deploy/README.md`
   (`gh attestation verify … --cert-identity …/build.yml@refs/heads/main
   --source-ref refs/heads/main --deny-self-hosted-runners`) before it is
   pinned.
4. **Step 2 (pin):** the kustomize base (`newTag` + `digest`), the three image
   lines of `base/deployment.yaml` and the Compose file shall name `0.3.0` and
   the verified digest; the README's image references (`:0.2.0…` examples)
   shall say `0.3.0`. `tests/test_deploy.py` shall pass.
5. A GitHub release `v0.3.0` (tag on the version-bump merge commit) shall
   summarise the changes since 0.2.0 — created by the operator or with their
   explicit go-ahead, since it's public.

## Acceptance Criteria

```gherkin
Given step 1 merged to main
When the build workflow finishes
Then ghcr.io/coreyjonoliver-labs/deal-hunter:0.3.0@sha256:<d> exists and the strict attestation verify passes for <d>

Given step 2
When the test suite runs
Then tests/test_deploy.py passes with every pin naming 0.3.0 and <d>
And kustomize build deploy/kubernetes/example renders all three containers as …:0.3.0@sha256:<d>
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `pyproject.toml`, `uv.lock` | edit | step 1: version 0.3.0 |
| `deploy/kubernetes/base/kustomization.yaml`, `deploy/kubernetes/base/deployment.yaml`, `deploy/compose/compose.yaml` | edit | step 2: tag + digest |
| `README.md` | edit | step 2: image examples |

## Data / State Impact

None in the repo. Deployments that pull 0.3.0 pick up the new prompts
(listings re-rate when seen again) and, if enabled, Discord.

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | `tests/test_deploy.py`; kustomize render |
| Coverage stays at or above the project's threshold | N/A | no threshold |
| Failure paths and edge cases are tested | Yes | the deploy test's existing failure cases |
| No secrets in code, config, tests, or fixtures | Yes | digests are public |
| External input is validated before use | N/A | |
| No injection vector | N/A | |
| Errors fail closed and don't leak internals | N/A | |
| New dependencies justified and pinned | Yes | none; aimm stays at 0.10.2 |
| Public interfaces documented; docs updated | Yes | README image examples |
| Provenance verified before pinning | Yes | requirement 3 |

## Edge Cases

1. Another commit lands on `main` between the bump and the pin → its build
   republishes `:0.3.0` with a new digest; pin the digest of the build whose
   provenance you verified (any `main` build of 0.3.0 is acceptable; prefer the
   latest verified one) and say which in the PR.

## Testing Strategy

- **Unit:** `tests/test_deploy.py` (existing).
- **Manual:** the strict attestation verify; kustomize render.

## Out of Scope

- The aimm upgrade (`bump-aimm`).
- Immutable release tags (`immutable-release-tags`).
- Switching any specific deployment to 0.3.0.

## End-to-End Verification

```sh
gh attestation verify oci://ghcr.io/coreyjonoliver-labs/deal-hunter@sha256:<d> \
  --repo coreyjonoliver-labs/deal-hunter \
  --cert-identity https://github.com/coreyjonoliver-labs/deal-hunter/.github/workflows/build.yml@refs/heads/main \
  --source-ref refs/heads/main --deny-self-hosted-runners
uv run pytest -q tests/test_deploy.py
kustomize build deploy/kubernetes/example | grep 'image:'
```

## Open Questions

- None.
