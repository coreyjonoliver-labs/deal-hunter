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
```
