# Deploying deal-hunter

deal-hunter runs anywhere Python or a container runs (see the main README).
This directory has starting points for running it unattended:

- `compose/`: Docker Compose, for one machine.
- `kubernetes/base/`: plain Kubernetes manifests (kustomize; no Helm, no
  operators). One pod: the monitor plus the dashboard, a volume, a
  dashboard-only Service and a NetworkPolicy.
- `kubernetes/components/kata/`: run the pod in a Kata Containers microVM
  (or gVisor: change the RuntimeClass name).
- `kubernetes/components/cilium-egress/`: a host-name egress allowlist and a
  node deny on the dashboard port, for clusters running Cilium.
- `kubernetes/components/prometheus/`: Prometheus metrics for any Prometheus
  Operator setup: a metrics port, a ServiceMonitor, alert rules (with
  `promtool` tests). See [Metrics and alerts](#metrics-and-alerts).
- `kubernetes/components/tracing/`: OpenTelemetry traces from the monitor to an
  OTLP/HTTP collector in your cluster (an env var and an egress rule). Opt-in
  and not in the example. See [Traces](#traces).
- `kubernetes/example/`: an overlay that puts them together. Copy it, fill in
  the settings, the image digest and the dashboard's host name, create the
  two Secrets named in `base/deployment.yaml`, then
  `kubectl apply -k <your overlay>`.

The base Service exposes the dashboard port only; the `prometheus` component
adds a second port for metrics.

Exposing the dashboard (an Ingress and an authenticating proxy) is left to
your cluster, since it depends on your ingress controller and login setup.
The dashboard has **no login**, so put it behind something that has one,
strip cookies on the way in (the pod runs a browser), and label the proxy's
namespace `deal-hunter/dashboard-access=true` (or narrow the NetworkPolicy
to the proxy's pods).

## Pinning the image

The image tag is the package version, and CI republishes it on every build of
`main`, so the tag moves. The manifests here pin the tag **and** a `sha256:`
digest, and the digest is what runs. `tests/test_deploy.py` fails if the
kustomize base, `base/deployment.yaml`'s image lines, the Compose file, or
the example overlay's own `images:` entry (if it has one) lack a digest,
disagree on the tag or digest, or name a version newer than
`pyproject.toml`'s. The pins may lag the package version, because a new
version's image only exists once `main` has built it.

To find the digest for a release, take the `pushed ...@sha256:...` line from
the `build` workflow's log for that `main` build, or the digest shown on the
package's page on GHCR. Then verify it was built by this repository's
`main` build workflow before you trust it:

```sh
gh attestation verify \
  oci://ghcr.io/coreyjonoliver-labs/deal-hunter@sha256:<digest> \
  --repo coreyjonoliver-labs/deal-hunter \
  --cert-identity https://github.com/coreyjonoliver-labs/deal-hunter/.github/workflows/build.yml@refs/heads/main \
  --source-ref refs/heads/main \
  --deny-self-hosted-runners
```

`--repo` alone accepts an attestation from any workflow on any branch of the
repository; the other flags pin the workflow, the branch and the runner type.

The tag is informational: the digest is what is pulled, and nothing checks
that the tag points at it. Each digest is pinned in the base, the Compose
file and the literal image lines of `base/deployment.yaml`; the example
overlay inherits the base's pin. If your overlay has its own `images:`
entry, set both `newTag` and `digest` in it: an entry that sets only
`newTag` silently drops the digest. Check with
`kustomize build <your overlay> | grep image:`; every line should end in
`@sha256:...`.

To move to a new version, publish it first (merge the version bump; `main`
builds and attests it), then verify its digest as above and update the tag
and digest in every file above in one follow-up change.

Pins only move forward: the test can't tell a lagging pin from a rollback to
an older (attested, possibly vulnerable) build, so a pinning change that
lowers the tag should be rejected in review. Nothing in CI checks that a
pinned digest is attested either, so whoever reviews a pinning change should
re-run the strict `gh attestation verify` above on the new digest. A pinned
digest stops being tagged on GHCR as soon as `main` rebuilds that version;
don't let a package cleanup policy delete untagged versions that `deploy/`
(or your own copy of it) still references.

## Isolation: do I need Kata?

Not to make it work. A plain container (runc) runs deal-hunter fine. The
question is what a browser exploit could reach.

In a container, Chromium's own sandbox can't start: it needs user
namespaces, which the default seccomp profile blocks for a container with
no capabilities. So the settings turn it off (`browser_sandbox = false`),
and the browser renders seller-written Facebook pages as the monitor's
user.

- **Inside the pod**, a browser exploit can read the API key, the notifier
  tokens and the Facebook session in every setup, Kata included. The
  egress allowlist is what limits where they can be sent. A spending limit
  on the API key limits what a stolen key costs.
- **Beyond the pod**, the next step would be a kernel exploit. Under runc
  that's the node's shared kernel: other pods, the kubelet, and whatever
  the node can reach. Under Kata (or gVisor) it's a throwaway guest kernel.

So, in order of preference:

1. **A microVM runtime plus an egress allowlist** (`kata` +
   `cilium-egress`): the strongest of these.
2. **runc with Chromium's sandbox restored**: a `Localhost` seccomp profile
   that allows the user-namespace `clone`/`unshare` calls Chromium makes,
   installed on each node, and `browser_sandbox = true`. That puts two
   layers (Chromium's sandbox and the container) in front of the kernel.
   Untested here: measure that Chromium starts before relying on it.
3. **runc with the sandbox off**: acceptable on a machine that runs nothing
   else you care about, with an egress filter and a spend-limited key.

Whatever the runtime, if you suspect the browser was compromised, delete the
data volume (aimm reloads its cache on every start), revoke the Facebook
session and rotate the API key. If you use Discord alerts, also delete and
recreate the Discord webhook: its URL is a credential that lets anyone post
to the channel. See the main README.

## Egress

The monitor needs `www.facebook.com`, `facebook.com`, `*.facebook.com`,
`*.xx.fbcdn.net` and `*.*.fna.fbcdn.net` on 443, plus your AI provider
(`api.anthropic.com`, `api.openai.com`) and notifier (`ntfy.sh`,
`api.pushover.net`, `api.telegram.org`, `api.pushbullet.com`, `discord.com`
for `notify.methods = ["discord"]` (webhook in `DISCORD_WEBHOOK_URL`), or your
SMTP server), and, if you turn on [traces](#traces), your OTLP collector. The
dashboard makes no outbound calls.

`discord.com` is optional and widens the allowlist more than the others: the
policy covers the whole pod, so a compromised browser that can reach
`discord.com` has a place to send stolen data to (any Discord webhook) or take
commands from (any bot), and `discord.com` sits behind Cloudflare, so an
allowlist by resolved IP also admits other sites on the same edge. If you
enable it, prefer an egress proxy that checks the TLS server name. Webhook
URLs on `discordapp.com`, `ptb.discord.com` and `canary.discord.com` are
accepted and sent to `discord.com`, which is the one host to allow.

The base NetworkPolicy can only say "HTTPS to public addresses", so the
browser can still reach any public site. Add the `cilium-egress` component
on Cilium, or your CNI's or firewall's equivalent (Calico's DNS policies, an
egress proxy), to narrow it to the hosts above.

## Traces

`deal-hunter run` exports OpenTelemetry traces (one per watch round: phrase
searches, page loads, listing fetches, AI calls, notifications) when
`OTEL_EXPORTER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) is set,
over OTLP/HTTP with protobuf. It is off otherwise, and `OTEL_SDK_DISABLED=true`
turns it off whatever else is set. The image has the OpenTelemetry packages
installed (the `tracing` extra). The main README ("Traces with OpenTelemetry")
lists the spans and what they carry: names, timings and counts, never a
phrase, location, listing text, URL, AI text, secret or exception message.
Only the `monitor` container is traced.

- **Compose:** put `OTEL_EXPORTER_OTLP_ENDPOINT=https://<collector>:4318` in the
  `.env` file the `monitor` service already reads (and
  `OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer ...` there, if the collector
  needs a token). `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` and
  `OTEL_TRACES_SAMPLER` work as in any OpenTelemetry service.
- **Kubernetes:** add `../components/tracing` to your overlay's `components:`
  (after `cilium-egress`: that component replaces the base policy's egress
  rules, so a rule added before it is lost). It sets
  `OTEL_EXPORTER_OTLP_ENDPOINT` on the `monitor` container to a placeholder
  (`http://otel-collector.observability.svc.cluster.local:4318`; change it, by
  editing the component or patching your overlay) and adds one NetworkPolicy
  egress rule to port 4318 for pods labelled
  `app.kubernetes.io/name: opentelemetry-collector` in the `observability` namespace
  (change both selectors to match your collector).
  With a token, use an `https://` endpoint or an in-cluster `.svc` address: `run`
  exits with code 2 for a token over plain http to any other host. A token for
  the collector goes in `OTEL_EXPORTER_OTLP_HEADERS` in the
  credentials Secret the monitor already reads, never in a manifest. The collector
  itself, and whatever stores traces, are yours to run.
- **Egress allowlist:** the collector's host is a new destination for the pod.
  With the `cilium-egress` component, add it to **both** lists in `cnp.yaml`
  (the DNS rule and `toFQDNs`) with port 4318 (or 443 behind a TLS endpoint);
  for an in-cluster collector, allow it by endpoint selector instead. For a
  collector outside the cluster, prefer an `https://` endpoint, and remember
  that a compromised browser shares the pod's network policy, so every host on
  the list is a host it can reach.
- If the collector is unreachable the monitor is unaffected: export errors are
  printed at most once a minute and the flush before a restart gives up after
  5 seconds.

## Metrics and alerts

`kubernetes/components/prometheus/` turns on `deal-hunter web --metrics-port
9741` and adds:

- a `metrics` port on the Pod and the Service (`9741`, `/metrics` only; see the
  main README for the metric list and the counter-reset note);
- a NetworkPolicy ingress rule on `9741` from namespaces labelled
  `deal-hunter/metrics-access=true`. Label the namespace your Prometheus runs
  in. The dashboard port keeps its own rule (`deal-hunter/dashboard-access`),
  so admitting the scraper to metrics doesn't admit it to the dashboard, and
  the metrics port serves nothing but `/metrics`. On Cilium, the host
  entities aren't denied on `9741` (they are on `8740`): the metrics hold no
  listing text, phrase, location or secret, but narrow it if you disagree;
- a `ServiceMonitor` (60 s interval) and a `PrometheusRule`. Your Prometheus
  must select them (many installs need a `release: <name>` label on both: add
  it from your overlay with `labels:`) and be allowed to reach the pod. Add
  the component to your overlay's `components:`, and keep `--metrics-port
  9741` in the `web` args if your overlay replaces them, as the example does
  for `--allow-host`: a replacement drops the argument the component adds.

The rules, in `prometheusrule.yaml`:

| Alert | Severity | Fires when |
|---|---|---|
| `DealHunterSearchStalled` | critical | no search round returned listings for longer than the configured `max_interval` plus 30 minutes, or the heartbeat metric is missing (10 minutes) |
| `DealHunterSessionExpiringSoon` | warning | the Facebook login cookie expires within 3 days |
| `DealHunterSessionExpired` | critical | the cookie has expired, or no session was ever saved (never for `facebook.anonymous`) |
| `DealHunterSearchFailures` | warning | more than 50% of phrase searches returned nothing over an hour, with at least 4 searches |
| `DealHunterAISpendHigh` | warning | estimated AI spend over 24 hours is above the threshold in the rule (default $1.00; the one number to change) |

These alerts are computed from files the monitor writes (its cache and
events file), so they are an operations aid, not a security control: under the
untrusted-browser model a compromised monitor can forge them or keep them quiet.

The rules set only a `severity` label. The heartbeat follows the
`<app>_last_success_timestamp_seconds` convention, so a generic "heartbeat
stale" rule in your monitoring setup can cover it too. Test the rules with
`promtool test rules` (`tests/test_deploy.py` runs `rules.test.yaml` when
`promtool` is installed), and render the example with
`kustomize build deploy/kubernetes/example`.
