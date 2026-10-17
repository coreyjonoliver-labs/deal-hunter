# Lessons archive

Rules demoted from, or retired out of, `.claude/rules/lessons.md` by
`/retrospect`. Kept, not deleted. A later log entry can bring one back.

## 2026-10-16 retrospect

### Demoted (below the cap)

- **Provenance checks pin the signer and the ref.** Use `--cert-identity …/build.yml@refs/heads/main` and `--source-ref`, not `--repo` alone. Every copy-pasteable image reference pins `:<version>@sha256:<digest>`, including the README examples, and the release pin step updates them. Also documented in `deploy/README.md`. *log 10-15 deploy/README.md:39; 10-16 README.md:467 (recurrence of the release-0-3-0 LOW)*
- **When a patch infers loop position from call counts, mirror every enclosing aimm loop** (cities around phrases). *log 10-15 launch.py:241*

### Retired: mechanized

- **Pins may lag the package version; only a pin ahead of the package fails.** retired: 2026-10-16. Enforced by `tests/test_deploy.py::test_version_bump_can_merge_before_its_image_is_pinned`. *log 10-15 _check_ref*
- **A kustomize `images:` entry with `newTag` but no `digest` strips the inherited digest.** retired: 2026-10-16. Enforced by `tests/test_deploy.py::test_overlay_tag_only_entry_fails`. *log 10-15 example kustomization*
- **A scanner's config and ignore list come from outside the PR's tree, with inline allows disabled.** retired: 2026-10-16 for gitleaks. Enforced by `.github/workflows/security.yml` `secret-scan` (`--config` and `--gitleaks-ignore-path` under `$RUNNER_TEMP`, `--ignore-gitleaks-allow`). A new scanner still needs the same treatment. *log 10-15 security.yml*
