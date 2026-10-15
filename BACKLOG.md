# Backlog

The work queue. `/next-task` takes the first unchecked item in **Current Focus**;
when that's empty it promotes from **Up Next**. Keep items small enough to be one
spec each.

## Current Focus

- [ ] pin-image-digest — pin the published 0.2.0 digest in `deploy/` and test
  that it stays in step with the package version
- [ ] live-watch-smoke-test — run a non-mini-PC `[[watch]]` against Facebook end
  to end (operator-run: needs a saved session and an API key)

## Up Next

- [ ] graduate-established — `/graduate` hardening for the established profile:
  branch protection requiring CI on `main`, CODEOWNERS, sandbox
  `failIfUnavailable` (operator applies settings/protection)
- [ ] immutable-release-tags — CI republishes the version tag on every `main`
  build; publish per-commit tags or refuse an existing version
  (workflow change: operator-only)
- [ ] chromium-sandbox-seccomp — Research: a `Localhost` seccomp profile that
  lets Chromium's sandbox start under runc, so `browser_sandbox = true` works in
  containers without a microVM

## Later

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
