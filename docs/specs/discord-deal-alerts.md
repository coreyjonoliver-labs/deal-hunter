# Spec: Discord deal alerts

**Status:** Ready
**Kind:** Implementation
**Author:** Corey Oliver
**Date:** 2026-10-15
**Phase:** Phase 3 — Alerting & observability
**Depends on:** None

---

## Summary

Add Discord as a notification method: when a listing rates at or above its
watch's rating, deal-hunter posts a card to a Discord webhook with the photo,
price, rating, AI summary, watch and link.

## Goal

As a buyer, I want good deals to show up in a Discord channel I already watch,
as a readable card with the photo and the AI's verdict, so that I can decide in
seconds without opening the dashboard.

## Background

Notifications are aimm's job today: `[notify] methods` maps to aimm's own
notifiers (email, ntfy, Pushbullet, Pushover, Telegram), rendered onto the aimm
`[user]` section by `render.py`. aimm 0.10.2 has no Discord notifier, and its
notifiers are dataclass mixins on `UserConfig` that `NotificationConfig.notify_all`
discovers via `__subclasses__()`, so adding one inside aimm means mutating its
class hierarchy.

The settled pattern (CLAUDE.md, decision 1) is to wrap aimm, not fork it:
`launch.py` patches a few aimm functions, each with a patch-point test. The
right patch point here is `NotificationConfig.notify_all`, which `User.notify`
calls with `(config, listings, ratings, statuses, force=..., logger=...)` for
the listings that passed the rating threshold. Its boolean return decides
whether aimm records the listings as notified (`User.to_cache`) and bumps the
"notifications sent" counter — so Discord has to report through it, or aimm
would re-send the same listing every cycle. `notify_all` recurses into
subclasses via `subclass.notify_all(...)`, and subclasses inherit a patched
classmethod, so the Discord send must happen only on the top-level call
(`cls is NotificationConfig`), never once per subclass.

Listing text (title, description) and the AI's summary of it are
seller-controlled and untrusted (conventions skill, Security). In Discord they
could carry `@everyone` pings, masked links (`[looks safe](https://evil)`) or
formatting abuse. The webhook URL is a bearer credential: anyone holding it can
post to the channel.

Operational alerts can already reach Discord in a cluster that runs
Alertmanager with a Discord receiver; deal alerts are a different stream (a buyer's channel, rich listing cards, not
incidents) and deliberately don't go through Prometheus. Ops alerting is the
`ops-metrics` spec.

## Requirements

1. The settings shall accept `"discord"` in `notify.methods`. The webhook URL
   shall come only from the `DISCORD_WEBHOOK_URL` environment variable, never
   from the settings file, and `required_env` shall list it when the method is
   enabled.
2. When `run` starts with the `discord` method enabled, it shall validate the
   URL as `https://` on `discord.com`, `discordapp.com`, `ptb.discord.com` or
   `canary.discord.com` with path `/api/webhooks/<numeric id>/<token>`, and
   shall exit with a `SettingsError` that does not echo the URL if it doesn't
   match.
3. The rendered aimm config shall not contain the webhook URL or any Discord
   field (aimm doesn't know the method).
4. When aimm's top-level `NotificationConfig.notify_all` runs with Discord
   enabled, deal-hunter shall post one embed per listing whose status isn't
   `NOTIFIED` (or every listing when `force` is set), batched at most 10
   embeds and 6,000 embed characters per message, and shall return
   `upstream_result or discord_ok`, where `discord_ok` is true only if every
   batch was accepted. Nested `notify_all` calls on subclasses shall not post.
5. Each embed shall contain: the listing title (as the embed title, truncated
   to 256 characters), the Marketplace item URL as the embed URL only if it
   matches `https://www.facebook.com/marketplace/item/<digits>/`, the price,
   the condition, the listing's town, the AI rating as stars and `n/5`, the AI
   summary (truncated to 1,000 characters), the watch name, the reason (new /
   price drop / updated / reminder, from the notification status), and the
   listing photo as the thumbnail only if it is an `https` URL on `fbcdn.net`
   (the dashboard's `image_url` rule). It shall not contain the description
   body or any URL other than those two.
6. Every message shall set `allowed_mentions` to `{"parse": []}`, and all
   seller- or AI-derived text shall have Discord markdown escaped (`\ * _ ~ `
   | > # - [ ] ( )` and leading `@`) so it renders literally: no pings, no
   masked links, no formatting injection.
7. Posting shall use a 10-second timeout and shall not follow redirects. On HTTP
   429 it shall wait `retry_after` (capped at 60 s) and retry, up to 3
   attempts per batch; on 5xx or a network error it shall retry with backoff
   (1 s, 4 s); on any other 4xx it shall not retry and shall log an error that
   names the status (404 → "webhook deleted or wrong") without the URL.
8. The webhook URL and its token shall never appear in logs, the events file,
   the dashboard or exceptions: `events.secret_values()` shall treat env vars
   ending in `WEBHOOK_URL` (and `WEBHOOK`) as secret, and error messages shall
   name the webhook only as "the Discord webhook".
9. A new command `deal-hunter notify-test` shall send one clearly-labelled
   sample card ("deal-hunter test notification", a fixed fake listing, no
   network fetch of a photo) through every enabled deal-hunter-owned method
   (Discord), and exit 0 on success or 1 with the reason.
10. `deploy/` shall list `discord.com` as an egress host when Discord is used:
    the cilium-egress component (both the DNS rule and `toFQDNs`, commented
    as optional), `deploy/README.md`'s egress list, and the README's notifier
    table (`notify.methods = ["discord"]` → `DISCORD_WEBHOOK_URL`).

## Acceptance Criteria

```gherkin
Given notify.methods = ["discord"] and DISCORD_WEBHOOK_URL is a valid webhook URL
When the monitor rates a listing at or above its watch's rating
Then exactly one message is POSTed to the webhook with one embed for that listing
And aimm records the listing as notified, so the next cycle sends nothing

Given two watches and aimm's recursive notify_all
When notify_all runs once for a batch of 3 listings
Then exactly one POST is made (not one per NotificationConfig subclass)

Given a listing titled "@everyone FREE [click](https://evil.example) **NOW**"
When it is sent to Discord
Then the payload has allowed_mentions {"parse": []} and the title text is escaped so it renders literally

Given the webhook answers 429 with retry_after 2, then 204
When a batch is sent
Then deal-hunter waits ~2 s, retries once, and reports success

Given the webhook answers 404
When a batch is sent
Then there is no retry, notify_all reports failure for Discord, and the logged error and events file contain neither the URL nor its token

Given DISCORD_WEBHOOK_URL=https://example.com/api/webhooks/1/x
When `deal-hunter run` starts
Then it exits 2 with a message that doesn't contain the URL

Given 23 listings in one notify call
When they are sent
Then 3 messages are POSTed (10 + 10 + 3 embeds), each under 6,000 embed characters

Given a valid webhook
When the operator runs `deal-hunter notify-test`
Then one sample card arrives and the command exits 0
```

## Interfaces & Files

| File | Change | Notes |
|---|---|---|
| `src/deal_hunter/discord.py` | create | payload building (escape, truncate, batch), sender (urllib, no redirects, retries) |
| `src/deal_hunter/launch.py` | edit | patch `NotificationConfig.notify_all` (top-level only); docstring list of fixes |
| `src/deal_hunter/settings.py` | edit | `discord` method; deal-hunter-owned methods kept out of aimm's `NOTIFIERS` mapping |
| `src/deal_hunter/render.py` | edit | `required_env` includes `DISCORD_WEBHOOK_URL`; nothing Discord rendered |
| `src/deal_hunter/events.py` | edit | `WEBHOOK(_URL)` env names are secret |
| `src/deal_hunter/cli.py` | edit | URL validation at `run`; `notify-test` command |
| `tests/test_discord.py` | create | payload, escaping, batching, retries (fake HTTP server or injected opener) |
| `tests/test_launch.py` | edit | patch-point test for `notify_all`; single-post under recursion; notified-cache behavior |
| `README.md`, `src/deal_hunter/example-settings.toml` | edit | the method, env var, what a card shows, `notify-test` |
| `deploy/README.md`, `deploy/kubernetes/components/cilium-egress/cnp.yaml` | edit | `discord.com` egress (optional, commented) |

## Data / State Impact

| Entity / Store | Change | Details |
|---|---|---|
| aimm `user-notifications` cache | unchanged semantics | written by aimm when `notify_all` reports success, as for its own methods |

## Quality & Security Checklist

| Item | Applies? | How this spec satisfies it |
|---|---|---|
| Tests cover every acceptance criterion | Yes | `tests/test_discord.py`, `tests/test_launch.py`, `tests/test_cli.py` |
| Coverage stays at or above the project's threshold | N/A | no threshold; every branch of the sender tested |
| Failure paths and edge cases are tested | Yes | 429, 404, 5xx, timeout, redirect, oversize batches, empty batch |
| No secrets in code, config, tests, or fixtures | Yes | URL from env only; fixtures use a fake webhook id/token; redaction extended |
| External input is validated before use | Yes | webhook URL shape; item URL and image URL allowlists |
| No injection vector | Yes | markdown escaping, `allowed_mentions` none, no masked links possible |
| Errors fail closed and don't leak internals | Yes | invalid URL stops `run`; errors never echo the URL |
| New dependencies justified and pinned | Yes | none: stdlib `urllib` |
| Public interfaces documented; docs updated | Yes | README, example settings, deploy README |
| aimm patch has a patch-point test | Yes | signature and recursion behavior of `notify_all` pinned |
| Repo stays free of personal deployment details | Yes | example webhook URLs are fake |

## Edge Cases

1. Discord down for a whole cycle → `notify_all` returns the other methods'
   result; if Discord is the only method, aimm doesn't mark the listings
   notified and retries next cycle (at most one card per listing per cycle).
2. Discord accepted batch 1 of 3, then failed → report failure so all are
   retried next cycle; a duplicate card for batch 1 is the accepted cost
   (no partial-success bookkeeping in aimm's cache).
3. A listing with no photo or a non-fbcdn photo → no thumbnail.
4. A title or summary of only markdown characters → escaped text, never an
   empty embed (fall back to "(untitled)").
5. Discord plus aimm methods enabled → both send; aimm marks notified if any
   succeeded.

## Testing Strategy

- **Unit:** payload builder (escaping table, truncation, batching by count and
  characters, field contents, URL allowlists), URL validation, redaction.
- **Integration:** sender against a local `http.server` fake (statuses, delays,
  redirects); the `notify_all` patch against aimm's real `NotificationConfig`
  and `User.notify` with a temp cache.
- **Manual:** `deal-hunter notify-test` to a real test channel; one live run
  producing a real card.

## Out of Scope

- Per-watch channels (one webhook for all watches; each card names its watch).
  A later spec can add `watch.discord_webhook_env`.
- Discord bots, threads, buttons or slash commands.
- Operational alerts (stalled monitor, expired session) — `ops-metrics`.
- Rewriting aimm's other notifiers.

## End-to-End Verification

```sh
export DISCORD_WEBHOOK_URL=...   # a test channel
uv run deal-hunter -s deal-hunter.toml notify-test        # one sample card, exit 0
uv run pytest -q tests/test_discord.py tests/test_launch.py
uv run deal-hunter -s deal-hunter.toml run                # a real match → one card, no repeat next cycle
```

## Open Questions

- Should a price-drop re-notification (`LISTING_DISCOUNTED`) post a new card or
  edit the original message (needs `?wait=true` and storing message ids)? This
  spec posts a new card labelled "Price drop".
