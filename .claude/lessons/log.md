# Lessons log

Immutable, append-only. One entry per material review finding; `/retrospect`
consolidates recurring ones into rules.

```yaml
- date: 2026-10-15
  source: "pin-image-digest / tests/test_deploy.py:25 / code-reviewer"
  importance: 5
  mechanizable: yes
  scope: unclassified
  category: build-test
  module: "tests/test_deploy.py"
  tags: [regex, manifest-parsing, kustomize]
  finding: "A text check over a structured file must anchor on the entry it means (e.g. the images: item whose name is the image), not the first matching key anywhere, or an unrelated entry satisfies it."

- date: 2026-10-15
  source: "pin-image-digest / tests/test_deploy.py:68 / code-reviewer"
  importance: 4
  mechanizable: yes
  scope: unclassified
  category: build-test
  module: "tests/test_deploy.py"
  tags: [fail-closed, findall]
  finding: "A consistency test that loops over matches must assert the expected number of matches; zero matches must fail, not pass vacuously."

- date: 2026-10-15
  source: "pin-image-digest / deploy/README.md:39 / security-auditor"
  importance: 5
  mechanizable: no
  scope: unclassified
  category: security
  module: "deploy/README.md"
  tags: [supply-chain, attestation, gh-attestation-verify]
  finding: "Documented provenance checks must pin the signer workflow and ref (--cert-identity …/build.yml@refs/heads/main, --source-ref), not just --repo, which accepts any workflow on any branch of the repo."

- date: 2026-10-15
  source: "pin-image-digest / deploy/kubernetes/example/kustomization.yaml:18 / security-auditor"
  importance: 5
  mechanizable: yes
  scope: unclassified
  category: security
  module: "deploy/kubernetes"
  tags: [kustomize, digest-pinning, supply-chain]
  finding: "A kustomize images: entry that sets newTag without digest strips an inherited digest; shipped overlays should either set both or omit the entry, and say so."

- date: 2026-10-15
  source: "pin-image-digest / tests/test_deploy.py:_check_ref / coordinator (missed by code-reviewer and security-auditor)"
  importance: 7
  mechanizable: no
  scope: unclassified
  category: process
  module: "tests/test_deploy.py"
  tags: [release, ci, chicken-and-egg, digest-pinning]
  finding: "A check that pins a build artifact must not require the artifact for the same change that produces it: CI's image job runs only after tests pass, so a test demanding the new version's digest at bump time blocks every release. Pins may lag; forbid only pins ahead of the package."

- date: 2026-10-15
  source: "investigate-search-failures / src/deal_hunter/events.py:53 / security-auditor"
  importance: 5
  mechanizable: yes
  scope: unclassified
  category: security
  module: "src/deal_hunter/events.py"
  tags: [log-filtering, untrusted-text, suppression]
  finding: "A filter that drops or reclassifies log records must key on the record's origin (module/funcName) and an anchored match, never an unanchored substring of the message: log messages embed seller-controlled text, so a listing title can otherwise suppress real errors."

- date: 2026-10-15
  source: "investigate-search-failures / src/deal_hunter/launch.py:241 / code-reviewer"
  importance: 4
  mechanizable: no
  scope: unclassified
  category: runtime-errors
  module: "src/deal_hunter/launch.py"
  tags: [aimm-patch, loop-structure]
  finding: "When a patch infers loop position from call counts, mirror every enclosing loop of the upstream code (aimm loops cities around phrases), not just the innermost one."

- date: 2026-10-15
  source: "investigate-search-failures / src/deal_hunter/events.py:82 / code-reviewer + security-auditor"
  importance: 4
  mechanizable: yes
  scope: unclassified
  category: security
  module: "src/deal_hunter/events.py"
  tags: [redaction, word-boundary]
  finding: "Redaction of short identifiers (slugs, ids) must match on token boundaries, not as bare substrings; otherwise a two-letter value garbles unrelated text and can hide what actually failed."

- date: 2026-10-15
  source: "rating-summary-format / aimm ai.py:347,474 (via profile.py rating prompts) / security-auditor"
  importance: 7
  mechanizable: yes
  scope: unclassified
  category: security
  module: "src/deal_hunter/launch.py"
  tags: [prompt-injection, parsing, aimm-patch, rating]
  finding: "Parsing a score out of model output that can contain seller text must anchor on the model's own marker (start of line) and take the first number after it; a greedy pattern lets a listing that mentions 'Rating 5' forge the score. Any prompt change that puts listing text on the rating line needs a forged-rating regression test."

- date: 2026-10-15
  source: "rating-summary-format / src/deal_hunter/summary.py:31 / security-auditor"
  importance: 5
  mechanizable: yes
  scope: unclassified
  category: security
  module: "src/deal_hunter/summary.py"
  tags: [sanitizing, markup, untrusted-text]
  finding: "A text cleaner that deletes characters can reassemble markup from fragments (ja**vascript → javascript); replace mid-word removals with a space, treat the output as plain text that still needs escaping, and escape after cleaning, never before."

- date: 2026-10-15
  source: "rating-summary-format / src/deal_hunter/launch.py:156 / code-reviewer"
  importance: 5
  mechanizable: yes
  scope: unclassified
  category: runtime-errors
  module: "src/deal_hunter/launch.py"
  tags: [parsing, fail-closed, silent-failure, alerts]
  recurrence_of: "2026-10-15 / forged rating via greedy parse (same parser, opposite failure)"
  finding: "Tightening a parser for safety must not make legitimate variants fail silently: accept the format's common drifts (case, separators, n/5) while keeping the anchor, and log when the old parser would have matched but the new one didn't — fail closed, but visibly."

- date: 2026-10-15
  source: "discord-deal-alerts / src/deal_hunter/discord.py:62 / code-reviewer (HIGH) + security-auditor (MEDIUM)"
  importance: 7
  mechanizable: yes
  scope: unclassified
  category: security
  module: "src/deal_hunter/discord.py"
  tags: [sanitizing, word-boundary, phishing, untrusted-text]
  recurrence_of: "2026-10-15 / redaction of short identifiers must match on token boundaries (boundary handling in text transforms, inverse direction)"
  finding: "A sanitizer that neutralizes something dangerous in untrusted text (URL schemes, mentions) must not depend on a word-boundary anchor: one prefixed character ('xhttps://', '_https://') slips past while the renderer still acts on it. Neutralize the dangerous token itself (every '://'), and test prefixed and adjacent forms."

- date: 2026-10-15
  source: "discord-deal-alerts / src/deal_hunter/discord.py:123 / code-reviewer + security-auditor"
  importance: 4
  mechanizable: yes
  scope: unclassified
  category: security
  module: "src/deal_hunter/discord.py"
  tags: [markdown, control-characters, bidi]
  finding: "Before escaping untrusted text for a markup renderer, collapse newlines where the field is meant to be one line and strip Unicode Cc/Cf characters (bidi overrides can visually reorder prices); escaping per-character doesn't cover line-start syntax like '1. ' lists."

- date: 2026-10-15
  source: "discord-deal-alerts / deploy/kubernetes/components/cilium-egress/cnp.yaml / security-auditor"
  importance: 5
  mechanizable: no
  scope: unclassified
  category: security
  module: "deploy/"
  tags: [egress, exfiltration, shared-cdn]
  finding: "Every host added to the pod-wide egress allowlist is also reachable by the unsandboxed browser: when a feature adds one (a notifier, an API), its docs must name it as a possible exfiltration channel (and shared-CDN fronting), and the compromise steps must include rotating that credential."

- date: 2026-10-15
  source: "discord-deal-alerts / src/deal_hunter/discord.py:357 / security-auditor"
  importance: 5
  mechanizable: yes
  scope: unclassified
  category: error-handling
  module: "src/deal_hunter/discord.py"
  tags: [fail-open, delivery, notifications, isolation]
  finding: "Isolating a bad item (drop one rejected card so it can't block the rest) must not turn 'nothing was delivered' into success: report success only when at least one item was actually accepted, and surface the rejection — otherwise a sender that rejects everything marks all work done and hides it."
- date: 2026-10-15
  source: "graduate-established / .github/workflows/security.yml (proposed) / security-auditor (HIGH)"
  importance: 6
  mechanizable: yes
  scope: unclassified
  category: security
  module: ".github/workflows"
  tags: [ci, secret-scan, gitleaks, self-configuring-check]
  finding: "A scanner run over a PR's tree must not take its config, ignore list or inline suppressions from that tree: write the config outside the checkout and disable inline allow comments (gitleaks: --config, --gitleaks-ignore-path, --ignore-gitleaks-allow), and prove it with a probe that tries to hide a planted secret."

- date: 2026-10-15
  source: "graduate-established / branch protection (proposed) / security-auditor (HIGH) + code-reviewer (MEDIUM)"
  importance: 6
  mechanizable: no
  scope: unclassified
  category: security
  module: ".github"
  tags: [branch-protection, required-checks, pull_request, spoofing]
  finding: "Required checks on pull_request run the PR's own workflow files, and bare context names accept any app's status: pin each required check to the GitHub Actions app_id, and state plainly that a PR editing workflows defines its own checks (review those diffs by hand, or use required workflows from the base branch)."

- date: 2026-10-15
  source: "graduate-established / .github/workflows/security.yml codeql (proposed) / code-reviewer + security-auditor (MEDIUM)"
  importance: 4
  mechanizable: yes
  scope: unclassified
  category: security
  module: ".github"
  tags: [codeql, code-scanning, false-gate]
  finding: "Requiring a CodeQL job only proves the analysis ran; analyze exits 0 with alerts. Blocking on alerts needs a code_scanning ruleset rule (or the code-scanning results check). Don't describe a run-and-report check as a gate."

- date: 2026-10-15
  source: "graduate-established / spec scope / security-auditor (HIGH)"
  importance: 5
  mechanizable: no
  scope: unclassified
  category: process
  module: "docs/specs"
  tags: [scope-honesty, publishing, workflow_dispatch]
  finding: "A hardening spec must list the channels it leaves open (here: workflow_dispatch publishing from any branch) and where they're tracked; claiming 'nothing lands unchecked' while a publish path bypasses the boundary overstates the control."

- date: 2026-10-15
  source: "graduate-established / .claude/settings.json (proposed) / security-auditor (HIGH)"
  importance: 5
  mechanizable: yes
  scope: unclassified
  category: security
  module: ".claude/settings.json"
  tags: [agent-permissions, gh, admin-token, sandbox-excluded]
  finding: "A command excluded from the sandbox runs with the operator's credentials; when that's an admin forge CLI, the calls that merge, publish or change protection must prompt (permissions.ask) even under a blanket Bash allow, or branch protection binds everyone but the agent."
- date: 2026-10-15
  source: "graduate-established / settings-delta permissions.ask (round 2) / security-auditor (HIGH)"
  importance: 5
  mechanizable: no
  scope: unclassified
  category: security
  module: ".claude/settings.json"
  tags: [agent-permissions, pattern-rules, overclaiming]
  recurrence_of: "2026-10-15 / admin forge CLI must prompt (same control, round 1)"
  finding: "Command-prefix permission rules are routable (implied POST via -f/--input, -XDELETE, global flags before the subcommand, printing the token and using curl); document them as a speed bump and put the boundary in the credential's scope, never in the pattern list."

- date: 2026-10-15
  source: "graduate-established / spec edge case 5 (round 2) / security-auditor (MEDIUM)"
  importance: 4
  mechanizable: no
  scope: unclassified
  category: security
  module: ".github/workflows/build.yml"
  tags: [publishing, push-trigger, workflow-self-edit]
  finding: "A publish guard written inside a workflow can't bind a branch that edits that workflow: any pushed branch runs its own copy with the job's permissions. Restricting publishing needs a control outside the file (environment-scoped credential with a branch policy, registry-side immutability)."

- date: 2026-10-15
  source: "graduate-established / spec acceptance criteria (round 2) / code-reviewer (MEDIUM)"
  importance: 3
  mechanizable: no
  scope: unclassified
  category: process
  module: "docs/specs"
  tags: [acceptance-criteria, server-side-config, verification]
  finding: "Server-side settings the test suite can't see (protection, rulesets, repo security toggles, local settings) each need an acceptance criterion with a live read-back command; otherwise half the requirements are unverifiable."
```

- date: 2026-10-16
  source: "ops-metrics / src/deal_hunter/dashboard.py SnapshotCache._refresh / code-reviewer + security-auditor (MEDIUM)"
  importance: 5
  mechanizable: no
  scope: unclassified
  category: error-handling
  module: "src/deal_hunter/dashboard.py"
  tags: [swallowed-exception, dead-state, stale-data, background-refresh]
  disposition: upheld
  finding: "A background refresh that keeps serving the last good value must surface its failure on the consumer path (a gauge, a flag, a one-line log); recording the error in state nothing reads is a swallowed exception."

- date: 2026-10-16
  source: "ops-metrics / src/deal_hunter/dashboard.py make_metrics_handler / security-auditor (MEDIUM)"
  importance: 5
  mechanizable: no
  scope: unclassified
  category: security
  module: "src/deal_hunter/dashboard.py"
  tags: [dns-rebinding, host-header, loopback-listener, scrape-endpoint]
  disposition: upheld
  finding: "Every HTTP listener that can bind loopback needs a Host check, scrape endpoints included; accepting IP literals plus the configured names keeps Prometheus (which scrapes by IP) working while refusing rebound DNS names."

- date: 2026-10-16
  source: "ops-metrics / deploy/kubernetes/components/prometheus/rules.test.yaml / code-reviewer (MEDIUM)"
  importance: 4
  mechanizable: yes
  scope: unclassified
  category: build-test
  module: "tests/test_deploy.py"
  tags: [promtool, skipped-test, unexecuted-test, ci]
  finding: "A test that skips when its tool is missing proves nothing until the tool runs somewhere; install the tool in CI (or fail rather than skip there) before relying on the test for an acceptance criterion."

- date: 2026-10-16
  source: "ops-metrics / docs/specs/ops-metrics.md / code-reviewer (MEDIUM)"
  importance: 4
  mechanizable: no
  scope: unclassified
  category: process
  module: "docs/specs"
  tags: [spec-drift, deviations]
  disposition: upheld
  finding: "An implementer-declared deviation from a requirement is folded into the spec text in the same change; a report-only deviation leaves the spec stale as the source of truth."

- date: 2026-10-16
  source: "otel-tracing / src/deal_hunter/tracing.py setup / security-auditor (HIGH)"
  importance: 7
  mechanizable: no
  scope: unclassified
  category: security
  module: "src/deal_hunter/tracing.py"
  tags: [third-party-sdk, logging, secrets, init-order, lastResort]
  disposition: upheld
  finding: "A third-party SDK that parses credentials from env may log them while parsing; install the redacting log handler (and propagate=False) before any SDK object is constructed, and validate the credential format yourself with a fixed, non-echoing error."

- date: 2026-10-16
  source: "otel-tracing / src/deal_hunter/tracing.py shutdown / security-auditor (MEDIUM)"
  importance: 5
  mechanizable: no
  scope: unclassified
  category: security
  module: "src/deal_hunter/tracing.py"
  tags: [bounded-shutdown, daemon-threads, logging, secrets]
  recurrence_of: "2026-10-16 / install the redacting log handler before SDK construction"
  disposition: upheld
  finding: "A timeout-bounded shutdown abandons worker threads that keep running; don't tear down the redacting log hook (or any safety state) at shutdown while abandoned work can still emit — keep it for the life of the process."

- date: 2026-10-16
  source: "otel-tracing / src/deal_hunter/tracing.py _parse / security-auditor (MEDIUM)"
  importance: 5
  mechanizable: no
  scope: unclassified
  category: security
  module: "src/deal_hunter/tracing.py"
  tags: [https-only, credentials, url-parsing, parser-differential]
  disposition: upheld
  finding: "A credential-bearing endpoint must be https unless loopback/in-cluster, and the host validated must be read by the same parser the transport connects with (or both parsers must agree); count every carrier (headers, userinfo, query) as a credential."

- date: 2026-10-16
  source: "otel-tracing / deploy/kubernetes/components/tracing/kustomization.yaml / security-auditor (MEDIUM)"
  importance: 4
  mechanizable: yes
  scope: unclassified
  category: security
  module: "deploy/kubernetes/components"
  tags: [networkpolicy, egress, untrusted-browser]
  finding: "An egress rule for the pod that hosts the untrusted browser names the destination pods (namespaceSelector AND podSelector in one `to` element), never a whole namespace."

- date: 2026-10-16
  source: "otel-tracing / docs/specs/otel-tracing.md / code-reviewer (HIGH)"
  importance: 6
  mechanizable: no
  scope: unclassified
  category: process
  module: "docs/specs"
  tags: [spec-drift, deviations]
  recurrence_of: "2026-10-16 / implementer-declared deviation folded into the spec (ops-metrics)"
  disposition: upheld
  finding: "The implementer prompt must say to fold any deviation into the spec text in the same change; a report-only deviation recurred within a day and became a HIGH."
