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
- `kubernetes/example/`: an overlay that puts them together. Copy it, fill in
  the settings, the image digest and the dashboard's host name, create the
  two Secrets named in `base/deployment.yaml`, then
  `kubectl apply -k <your overlay>`.

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
SMTP server). The dashboard makes no outbound calls.

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
