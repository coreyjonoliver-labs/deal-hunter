# nuc-hunter

Watches Facebook Marketplace near you for NUC-class mini PCs (Intel/ASUS NUC,
Beelink, Minisforum, GMKtec, Geekom and similar) and sends you only the good ones.

It's a thin layer over
[ai-marketplace-monitor](https://github.com/BoPeng/ai-marketplace-monitor)
(pinned to 0.10.2). The monitor does the browsing, de-duplication and
notifications. nuc-hunter turns a short settings file into a NUC-tuned config
for it:

- **Local pickup only**: searches use Facebook's local-pickup filter within your
  radius. Listings that say shipping-only or no-pickup are dropped, and the AI
  treats shipping-only or pay-before-pickup as a dealbreaker.
- **Searches**: `nuc`, `beelink`, `minisforum`, `mini pc`. Optionally also
  ThinkCentre Tiny, OptiPlex Micro and EliteDesk Mini (`include_tiny_pcs`).
- **Keyword gate**: a listing must name a mini-PC brand or form factor. Listings
  marked parts-only, broken, BIOS-locked, "looking for" or case-only are dropped
  before any AI cost. Links (`http://`, `https://`, `www.`) in the listing are
  ignored, so `nuc` hidden in a tracking URL doesn't let a string trimmer through.
- **AI judge**: a mini-PC-specific prompt. The AI works out the model and CPU
  from the model number (e.g. `NUC8i5BEH` → 8th-gen i5, `SER5` → Ryzen 5 5560U,
  `EQ12` → N100) and checks for RAM, SSD and power adapter. It estimates a fair
  used price, compares it to the asking price and looks for scam signals. Each
  notification ends with a summary like
  `Rating 4: NUC11 i5-1135G7, 16GB/512GB, fair ~$220 vs $150 ask — buy`.
- **No secrets on disk**: the generated config only holds `${ENV_VAR}`
  placeholders, which ai-marketplace-monitor fills in at runtime.

## Setup

```sh
uv sync --locked
uv run playwright install chromium
uv run nuc-hunter init          # creates nuc-hunter.toml (gitignored)
```

Edit `nuc-hunter.toml`:

1. **`location.search_city`**: open Marketplace in a browser, search for
   anything near you, and copy `XXX` from
   `facebook.com/marketplace/XXX/search?...`. This is a city slug like `seattle`
   or a numeric id.
2. Set `radius`, the price range and `preferences` (plain English; the AI judges
   listings against it).
3. Pick an AI provider and notifier, and export the environment variables
   they need:

| Setting | Env vars |
|---|---|
| `ai.provider = "anthropic"` (default, Haiku 4.5) | `ANTHROPIC_API_KEY` |
| `ai.provider = "openai"` | `OPENAI_API_KEY` |
| `ai.provider = "ollama"` / `"none"` | — |
| `notify.methods = ["ntfy"]` | `NTFY_TOPIC` |
| `["pushover"]` | `PUSHOVER_USER_KEY`, `PUSHOVER_API_TOKEN` |
| `["telegram"]` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| `["pushbullet"]` | `PUSHBULLET_TOKEN` |
| `["email"]` (+ `notify.email`) | `SMTP_PASSWORD` |
| Facebook login (optional; `nuc-hunter login` is better) | `FACEBOOK_USERNAME`, `FACEBOOK_PASSWORD` |

For email, set `notify.smtp_server` / `smtp_username` / `smtp_port` unless your
provider uses `smtp.<your domain>` on port 587 (Gmail does; use an app password).
`ntfy` uses `https://ntfy.sh` unless you set `notify.ntfy_server`.

`ntfy` is the easiest way to get phone alerts. Install the ntfy app, subscribe
to a long random topic name, and set `NTFY_TOPIC` to that name. Anyone who knows
the topic can read it, so treat it like a password.

## Run

```sh
uv run nuc-hunter run                 # renders build/aimm-config.toml, starts the monitor
uv run nuc-hunter run -- --verbose    # anything after -- is passed to ai-marketplace-monitor
```

### Log in to Facebook once

```sh
uv run nuc-hunter login
```

A Chrome window opens. Log in, including any 2FA or security check (you have
5 minutes). nuc-hunter saves the browser's cookies to
`~/.ai-marketplace-monitor/facebook-session.json` (owner-only; change it with
`facebook.session_file`), and every `run` starts already logged in: no login
form, no 60-second wait. The session is saved again after each login and
search, so Facebook's refreshed cookies are kept.

**That file is a full login to your Facebook account.** Anyone who copies it
is logged in as you, without your password or 2FA: Messenger, messaging
sellers in your name, Meta Pay, and anything linked in Accounts Center.

- Use a **dedicated Facebook account** for the monitor if you can, not your
  personal one.
- Log in only to Facebook in that window. nuc-hunter keeps only Facebook's
  cookies, but there's no reason to sign in to anything else there.
- Don't commit, share or back it up anywhere you wouldn't put your password.
  On a Mac, every save excludes it from Time Machine; check other backup or
  sync tools (iCloud Drive, Dropbox) don't cover `~/.ai-marketplace-monitor`.
- Deleting the file doesn't log it out. To revoke it (do this after any
  suspected leak, a lost backup disk included), log that session out under
  Facebook's *Settings → Accounts Center → Password and security → Where
  you're logged in*, then delete the file.
- Don't run with `DEBUG=pw:*`: Playwright's protocol log prints cookies.

When the saved session expires, `run` logs a warning and falls back to the old
behaviour: a Chrome window opens and you have 60 seconds to log in (that login
is saved too). Run `nuc-hunter login` again to replace it. On a machine with no
one to log in, set `facebook.require_session = true`: the monitor runs
headless, and exits with an error instead of waiting when the session is
missing or expired, or when Facebook logs it out mid-run.

Without a saved session, the first run opens a Chrome window and waits 60
seconds for you to log in. If you set `facebook.anonymous = true`, it runs
headless without logging in. That's less reliable because Facebook throttles
anonymous sessions sooner.

### See results and status

```sh
uv run nuc-hunter web          # http://127.0.0.1:8740/
```

A read-only page, refreshed every 5 minutes, with:

- **Results**: rated listings with photo, price, town, AI rating and summary,
  and whether you were notified. Tabs show matches (at or above your
  `ai.rating`), everything rated, or everything fetched. Listings your
  current filters would drop are hidden from the first two, so older ratings
  from before a settings change (say, a town you've since removed) don't
  show up as matches.
- **Status**: running, starting, or stalled (no search within
  `max_interval` plus 30 minutes), with the last start and search.
- **AI usage**: calls, tokens and estimated spend for 24 hours, 7 days and
  overall, at list prices.
- **Facebook login**: whether the saved session exists, when it was saved and
  when its login cookie expires.
- **Recent problems**: the last warnings and errors, plus aimm's lifetime
  counters and your settings.

`/api/summary.json` has the same data as JSON, and `/healthz` answers `ok`.
`web` runs separately from `run`, and only reads the monitor's files, so it
can stay up while the monitor restarts.

Activity, AI usage and status come from `nuc-hunter-events.jsonl`, which
`run` writes next to aimm's cache (aimm's own log has no timestamps), so
they start with the first run after updating. Results and counters come from
aimm's cache and include earlier runs.

The page has no login. It listens on 127.0.0.1, refuses requests for any
other host name and pages loaded by other websites, so other sites can't read
it through your browser. It never reads the Facebook session itself, only a
small `facebook-session.meta.json` in `~/.ai-marketplace-monitor` (when it
was saved, when it expires) that each save writes. Warnings shown on the page have secret-looking
environment values (API keys, tokens, ntfy topic) replaced with
`[redacted]`.

To reach it from other machines, keep it on 127.0.0.1 and put a proxy that
requires a login in front of it on the same machine (or the same pod), and
pass the proxy's host name with `--allow-host`. `--host 0.0.0.0` exposes it
to everything that can reach the port, whatever Host name it sends. In a
pod, mount the monitor's volume read-only for it, and don't give it the
session file or any secrets: set `facebook.session_file` to a directory on a
volume only the monitor mounts, since the default puts it in
`~/.ai-marketplace-monitor`. The dashboard's mount must not hold the session
or a `config.toml`. Use a proxy that buffers whole requests, since slow
clients can tie up the dashboard's 8 connections. `/healthz` answers any
Host for probes.

### Run in a container

The `Dockerfile` builds one image for both `run` and `web` (CI publishes it to
`ghcr.io/<owner>/nuc-hunter:<version>` with signed build provenance). The tag
is the package version and moves with every build of `main`, so pin the
digest (`:0.1.0@sha256:…`). It runs as uid 1000, works with a read-only root
filesystem, and keeps nothing in the image: settings, keys and state come
from outside. Under Kubernetes, run it as uid/gid 1000 with `fsGroup: 1000`
so it can write its volumes.

| | `run` | `web` |
|---|---|---|
| Settings file (pass `-s`) | read-only | read-only |
| `/home/nuc/.ai-marketplace-monitor`: aimm's cache, log, events (keep it) | read-write | **read-only** |
| The directory of `facebook.session_file` (keep it) | read-write | not mounted |
| `/work`: generated config, page dumps | scratch | not needed |
| `/tmp` | scratch | scratch |
| API key and notifier environment variables | yes | none |

Settings for a container, where no one is there to log in:

```toml
[facebook]
session_file = "/session/facebook-session.json"   # a volume only `run` mounts
require_session = true
browser_sandbox = false
```

Chromium's sandbox needs user namespaces, which container runtimes usually
block (measured: it doesn't start under Docker's defaults with no
capabilities), hence `browser_sandbox = false`. Without it, a browser bug on
a Facebook page runs as the monitor's user and can read its API keys. Make
the container the boundary: a microVM runtime (Kata, gVisor) if you have
one, outbound traffic limited to the hosts below, and an API key with a
spending limit.

A search and a listing page load from `www.facebook.com`,
`static.xx.fbcdn.net`, `scontent.xx.fbcdn.net` and
`<name>.<pop>.fna.fbcdn.net` (images from the nearest Facebook CDN site),
all on 443. Add your AI provider (`api.anthropic.com`, `api.openai.com`) and
notifier (`ntfy.sh`, `api.pushover.net`, …).

Log in on a desktop with `nuc-hunter login`, then give the container the
saved file read-only (a secret mount, or a bind mount) and pass it to `run`
with `--session-seed`. `run` copies it to `facebook.session_file` whenever its
content changes, and otherwise keeps the copy it has been refreshing. Don't
also use that session on the desktop while the container runs: one login
used from two places at once can get the account checkpointed.

With `--wait-for-session SECONDS`, a missing or expired session doesn't stop
`run`, whether it's found at start or Facebook logs the monitor out mid-run.
`run` records the problem (the dashboard shows it under recent problems)
and waits. With `--session-seed` it then checks the seed every SECONDS and
starts again only when the seed changes, so it doesn't keep loading Facebook
with dead cookies. Without a seed it retries every SECONDS. Replacing the
seed file is all it takes to recover. Without the option, `run` exits with
status 2.

`--restart-after SECONDS` does the same for any other error the monitor
stops on (a Facebook outage, a page that keeps failing to load): it waits,
doubling the wait after each consecutive failure up to 6 hours, and starts
again. Both options restart `run` as a fresh process with the same PID, so
nothing outside needs to restart it.

These commands run an unsandboxed browser with full network access: they
show the mounts, not the isolation. Add a microVM runtime and an egress
filter (above) before running it unattended.

```sh
docker run -d --name nuc-hunter --read-only --cap-drop ALL --tmpfs /tmp \
  --tmpfs /work:uid=1000,gid=1000 \
  -v nuc-aimm:/home/nuc/.ai-marketplace-monitor -v nuc-session:/session \
  -v ~/.ai-marketplace-monitor/facebook-session.json:/seed/facebook-session.json:ro \
  -v "$PWD/nuc-hunter.toml:/config/nuc-hunter.toml:ro" -e ANTHROPIC_API_KEY \
  ghcr.io/<owner>/nuc-hunter:0.1.0 -s /config/nuc-hunter.toml run \
  -o /work/build/aimm-config.toml \
  --session-seed /seed/facebook-session.json --wait-for-session 900 --restart-after 900

docker run -d --name nuc-hunter-web --read-only --cap-drop ALL --tmpfs /tmp \
  -v nuc-aimm:/home/nuc/.ai-marketplace-monitor:ro \
  -v "$PWD/nuc-hunter.toml:/config/nuc-hunter.toml:ro" -p 127.0.0.1:8740:8740 \
  ghcr.io/<owner>/nuc-hunter:0.1.0 -s /config/nuc-hunter.toml \
  web --host 0.0.0.0 --allow-host 127.0.0.1
```

The seed has to be readable by uid 1000 inside the container (on Linux,
the desktop file is owner-only: copy it into a secret instead).

`web` loads aimm's code without opening aimm's cache for writing, so a
read-only mount is enough. It treats the cache as untrusted (the monitor's
browser writes next to it): it decodes only plain data, never pickled
objects, and skips oversized rows. With `--host 0.0.0.0` the page answers
anyone who can reach the port (see "See results and status"): publish it
only on 127.0.0.1, or behind a proxy that requires a login.

**If you suspect the browser was compromised**, restarting isn't enough:
aimm loads its cache and any `config.toml` from
`~/.ai-marketplace-monitor` on every start, so something planted there
would run again. Delete that volume and the session volume, revoke the
session in Facebook's *Accounts Center → Password and security → Where
you're logged in*, rotate the API key, then save a new session.

### Tune the filters without hitting Facebook

```sh
uv run nuc-hunter check "Beelink SER5 Ryzen 5 16GB 500GB" --price 180
# [nuc] PASS (goes to AI)
uv run nuc-hunter check "Intel NUC for parts" "no power" --price 30
# [nuc] DROP: antikeyword "for parts"; price outside 40-400
```

`nuc-hunter render` writes the config without starting anything, so you can
read what's generated.

## Notes

- `run` passes `--no-webui` to ai-marketplace-monitor. Upstream's web UI has no
  login on 127.0.0.1, so any web page you visit could read its logs, which
  include your search location. A DNS-rebinding page could also edit the
  config to send your API keys elsewhere. Only use `nuc-hunter run --webui` if
  you accept that risk.
- Notification text includes the AI's summary and part of the listing. Both
  are partly written by the seller. The prompt tells the AI to treat listing
  text as untrusted, but don't act on payment or contact details that show up
  in an alert.
- Install with `uv sync --locked` to get the hash-pinned dependency set from
  `uv.lock`.
- ai-marketplace-monitor **also** reads `~/.ai-marketplace-monitor/config.toml`
  if it exists, and merges it in. Keep that file empty or remove it, or its
  items will run too.
- Searches run every 1–2 hours by default. Shorter intervals and more search
  phrases get the account throttled faster.
- Page loads time out after 60 seconds and are retried 10 times (upstream
  waits forever, so a sleep or network drop mid-load used to hang the monitor).
  A listing page that keeps failing is skipped. A search page that keeps
  failing stops the monitor with an error, so run it under something that
  restarts it, with a delay of several minutes between restarts. Without a
  saved session every start logs in to Facebook again, and a fast restart
  loop can get the account checkpointed. On a Mac, `caffeinate -i op run …`
  stops idle sleep.
- The browser runs with Chromium's sandbox on and without your API keys in its
  environment (Playwright's default is no sandbox and the full environment),
  so a browser bug on a Facebook page can't read your keys or the saved
  session as easily. If the sandbox can't start (some containers), set
  `facebook.browser_sandbox = false` and isolate the container instead. The
  browser also doesn't get `HTTP(S)_PROXY` or custom CA variables; use
  ai-marketplace-monitor's `proxy_server` setting if you need a proxy.
  `run` makes `~/.ai-marketplace-monitor` and the files in it owner-only.
- When it can't parse a results page, ai-marketplace-monitor saves it as
  `debug_<date>.html` in the current directory. With a saved session that's a
  logged-in page (your name and id), so nuc-hunter makes it owner-only and
  it's gitignored; delete them when you're done. They aren't cleaned up.
- `[Search] Failed to get search results` after every search is upstream
  noise: 0.10.2 logs it unconditionally. Listings after it are processed
  normally.
- Automating Facebook may break its Terms of Service. Use a modest interval
  and expect occasional CAPTCHAs.

## Development

```sh
uv run pytest
uv run ruff check . && uv run mypy
```

`tests/test_filters.py` runs real-looking listing titles through
ai-marketplace-monitor's own keyword matcher. `tests/test_render.py` loads the
generated config with its real `Config` class, so upstream schema changes show up
as test failures.
