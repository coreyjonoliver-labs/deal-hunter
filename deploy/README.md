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
   `cilium-egress`): the setup this project is run with.
2. **runc with Chromium's sandbox restored**: a `Localhost` seccomp profile
   that allows the user-namespace `clone`/`unshare` calls Chromium makes,
   installed on each node, and `browser_sandbox = true`. That puts two
   layers (Chromium's sandbox and the container) in front of the kernel.
   Untested here: measure that Chromium starts before relying on it.
3. **runc with the sandbox off**: acceptable on a machine that runs nothing
   else you care about, with an egress filter and a spend-limited key.

Whatever the runtime, if you suspect the browser was compromised, delete the
data volume (aimm reloads its cache on every start), revoke the Facebook
session and rotate the API key. See the main README.

## Egress

The monitor needs `www.facebook.com`, `facebook.com`, `*.facebook.com`,
`*.xx.fbcdn.net` and `*.*.fna.fbcdn.net` on 443, plus your AI provider
(`api.anthropic.com`, `api.openai.com`) and notifier (`ntfy.sh`,
`api.pushover.net`, `api.telegram.org`, `api.pushbullet.com`, or your SMTP
server). The dashboard makes no outbound calls.

The base NetworkPolicy can only say "HTTPS to public addresses", so the
browser can still reach any public site. Add the `cilium-egress` component
on Cilium, or your CNI's or firewall's equivalent (Calico's DNS policies, an
egress proxy), to narrow it to the hosts above.
