# Boondock Echo → Slack & Discord

Turns [Boondock Echo](https://boondockecho.com) keyword alert emails into
readable Slack or Discord messages, with the recording attached and playable in
place.

## Why

The Boondock Echo listens to a scanner, transcribes what it hears, and emails
you when the transcript contains a keyword. The email works, but it isn't
something you want to read forty times a day:

- it lands in a mailbox, not where the rest of your team is already looking
- the transcript is buried in a wall of labelled fields
- the recording is a link that expires, so the archive rots
- there's no good way to route different scanners to different audiences

This relays those emails into chat instead. Each alert becomes one message —
source, matched keyword, time, transcript, and the mp3 with an inline player:

```
🚒 Fire Dispatch  [vegetation fire]  — 05 Sep 2026 18:33
   > Engine 46, Engine 69, vegetation fire, 123 Fake Street
   ┌──────────────────────────────┐
   │ ▶  21-19-22.mp3       0:14   │
   └──────────────────────────────┘
```

The time shown is when the recording ended, taken from Boondock's own file
naming, not when the email was sent, so email and relay delays don't skew it.
It looks the same on Slack and Discord. The audio is uploaded rather than
linked, so it stays playable long after Boondock's presigned URL expires. One
alert can go to several channels, across several Slack workspaces and Discord
servers at once, and adding a destination is a config edit — no redeploy.

## What you need

None of this is included, and the hardware side is a separate purchase from a
separate vendor:

- **A scanner or two-way radio** with an audio output jack, and a cable to it
- **A Boondock Echo** — the device that listens, records, and transcribes
- **Keyword alert emails enabled on it.** The Echo records and plays back
  standalone, but transcription and keyword notification are cloud features.
  That means either a BoondockEcho.com subscription or your own self-hosted
  Boondock server — the hardware and server software are open source, so
  self-hosting is a real option. Check their current terms; this project has no
  visibility into their pricing and doesn't track it.
- **An AWS account**, and a **domain** you can add DNS records to
- **Somewhere to post**, one or both of:
  - a **Slack workspace** where you can create an app (some workspaces restrict
    this to admins)
  - a **Discord server** where you can add a bot (needs the Manage Server
    permission)

This project is only the last hop: it takes an alert email that already exists
and puts it in chat. It does no transcription and talks to no radio.

Not affiliated with, endorsed by, or supported by Boondock Technologies, Slack,
or Discord. Product names are used only to identify what this interoperates
with.

## How it flows

```
   ┌──────────┐     audio      ┌──────────────┐
   │ scanner  │───── REC OUT ─▶│ Boondock     │
   │          │                │ Echo         │
   └──────────┘                └──────┬───────┘
                                      │ transcribes, matches keywords
                                      ▼
                          alert email + mp3 attachment
                                      │
   ══════════════════════════════ AWS │ ════════════════════════════
                                      │
                          ┌───────────▼───────────┐
                          │ SES  email receiving  │  MX record points here
                          │  catch-all on domain  │
                          └───────────┬───────────┘
                                      │
                    ┌─────────────────┴─────────────────┐
                    │ 1. write raw MIME                 │ 2. invoke
                    ▼                                   ▼
             ┌─────────────┐                   ┌──────────────────┐
             │ S3  bucket  │◀──── get object ──│ Lambda           │
             │ inbound/    │                   │ ses_to_chat.py   │
             │ 30d expiry  │                   └────────┬─────────┘
             └─────────────┘                            │ read routing table
                                          ┌─────────────▼──────────────┐
                                          │ SSM Parameter Store        │
                                          │ /boondock/routes           │
                                          │ rules → {platform, token,  │
                                          │          channel}          │
                                          └─────────────┬──────────────┘
                                                        │ every matching route
   ═════════════════════════════════════════════════════│═══════════
                                   ┌────────────────────┴─────────────┐
                                   ▼                                  ▼
                    ┌──────────────────────────────┐   ┌──────────────────────┐
                    │ Slack                        │   │ Discord              │
                    │  files.getUploadURLExternal  │   │  POST message with   │
                    │  send mp3                    │   │  mp3 attached        │
                    │  files.completeUploadExternal│   │  (one request)       │
                    └──────────────────────────────┘   └──────────────────────┘
                              one message per channel, audio inline
```

SES can't hand a Lambda the message body — only headers — so the receipt rule
writes to S3 first and the function reads the object back. That's the reason
for the two-action rule and the bucket in the middle.

The last hop differs by platform. Slack can't attach a file to an existing
message, so there the upload *is* the post: the text rides along with the
upload's final step. Discord takes the text and the file in a single request.
Either way the alert and its audio arrive as one message rather than a thread.

Each step is retried on transient failures — timeouts, 5xx, rate limits — and
if the audio still can't be attached, the text posts anyway with Boondock's own
recording link. A failure in one channel never stops delivery to the others.

## Routing

All destinations live in one JSON document in SSM Parameter Store. Each entry
is a route: where to post, and which alerts to post there.

**Every matching route receives the alert**, not just the first. A route's key
is a name only — matching is explicit, through five optional fields:

| Field | Matches |
|---|---|
| `always: true` | everything — a catch-all channel |
| `addresses` | the recipient, by full address or local part |
| `keywords` | substring of the alert's `Keywords Matched`, case-insensitive |
| `sources` | the source name in the body, exact, case-insensitive |
| `transcript` | whole-word match in the transcript itself |

**Every field a route specifies must match**, and fields it omits are ignored.
So `sources` plus `keywords` means *this source AND one of these keywords* — a
route for one scanner's fire traffic doesn't also pick up fires from elsewhere,
or that scanner's medical calls. A route with only `keywords` matches on keyword
alone, from any source.

`keywords` trusts the device's own keyword matcher. `transcript` searches what
was actually said, so filtering can live here instead of in the device — set the
device to send every transmission and decide in this table. Transcript matching
is whole-word rather than substring: `sar` won't fire on "Caesar", but `fire`
won't fire on "firefighter" either.

`default` is the one reserved key — it fires only when nothing else matched, so
it stays a fallback rather than a second catch-all. Routes sharing a token and
channel are collapsed, so overlapping rules post once. A route with none of the
five matchers logs a warning at load, since it can never fire. Nothing matching
and no `default` means the alert is dropped rather than posted somewhere
arbitrary.

Display fields never affect matching: `label` overrides the source name the
device reports, and `emoji` is a shortcode prefixed to the message (default
`:rotating_light:`). Keys starting with `_` are ignored, so
`"_channel": "#fire-dispatch"` works as a comment.

### One scanner per address

Point each device at its own address and route on it:

```json
{
  "fire":    {"token": "xoxb-…", "channel": "C0AAA",
              "addresses": ["firedispatch@subdomain.dev.null"],
              "emoji": ":fire_engine:"},
  "police":  {"token": "xoxb-…", "channel": "C0BBB",
              "addresses": ["police@subdomain.dev.null"],
              "emoji": ":police_car:"},
  "default": {"token": "xoxb-…", "channel": "C0ZZZ"}
}
```

### One scanner, filtered by keyword

If every alert arrives at the same address from the same device, route on what
it matched:

```json
{
  "wildland":  {"token": "xoxb-…", "channel": "C0AAA",
                "keywords": ["vegetation fire", "smoke check"],
                "emoji": ":evergreen_tree:", "label": "Wildland"},
  "structure": {"token": "xoxb-…", "channel": "C0BBB",
                "keywords": ["structure fire"],
                "emoji": ":house:", "label": "Structure"}
}
```

Overlapping keywords fan out rather than compete: a `fire` route and a
`vegetation fire` route both receive a vegetation fire alert.

### Filtered channels, a catch-all, and two platforms

One scanner covering fires, medical, collisions and briefings can feed a channel
per category *and* a channel that sees everything — on Slack, on Discord, or
both:

```json
{
  "fire":     {"_channel": "#fire", "token": "xoxb-…", "channel": "C0AAA",
               "label": "Fire Dispatch", "emoji": ":fire_engine:",
               "keywords": ["vegetation fire", "structure fire"]},
  "briefing": {"_channel": "#briefing", "token": "xoxb-…", "channel": "C0BBB",
               "label": "Fire Dispatch", "emoji": ":clipboard:",
               "keywords": ["morning briefing", "fire weather"]},
  "all":      {"_channel": "#dispatch-all", "token": "xoxb-…", "channel": "C0CCC",
               "label": "Fire Dispatch", "emoji": ":radio:", "always": true},
  "aid":      {"_channel": "#mutual-aid (club Discord)", "platform": "discord",
               "token": "MTIz…", "channel": "1234567890123456789",
               "emoji": ":handshake:", "keywords": ["structure fire"]}
}
```

A structure fire posts to `fire`, `all`, and the Discord server's `aid`
channel. A medical call posts only to `all`.

This also makes migrating channels safe: give the old and new channels both
`"always": true`, confirm the new one looks right, then delete the old entry.
Nothing goes dark in between.

The table is cached for five minutes, so edits apply without a redeploy. The
receipt rule matches the whole domain, so a new scanner needs no AWS change
either: add a route and point the device at the address.

## Slack and Discord

Each route takes an optional `platform`: `slack` (the default) or `discord`.
Everything above — parsing, routing, matching, fan-out — is shared; only the
sender and the per-route credentials differ.

| | Slack | Discord |
|---|---|---|
| Bot | an app with bot scopes `chat:write` and `files:write` | an application with a bot, invited with **Send Messages** and **Attach Files** |
| `token` | Bot User OAuth Token, `xoxb-…` | bot token from the Developer Portal |
| `channel` | channel ID like `C09ABCDEFGH` | numeric ID, 17–20 digits (Developer Mode → Copy Channel ID) |
| One bot per | workspace | application, invited to any number of servers |
| Message text limit | ~3,000 characters | 2,000 characters |
| File size limit | very large | low, varies with server boost level |

Neither platform accepts a channel *name*; the routing table rejects one at
load rather than failing mid-post.

Messages look the same on both. Inline code, italics, blockquotes and standard
emoji shortcodes share syntax; bold and escaping differ, and the code handles
both per platform. Custom emoji don't port — Slack's are workspace-scoped and
Discord's need `<:name:id>` syntax — so stick to standard shortcodes for routes
that might point at either.

Dispatch clips run ~100 KB, so Discord's file cap doesn't come up; long
recordings would hit it. Long transcripts are trimmed to fit each platform's
text limit.

## Who can post to your channels

The envelope sender is trivially forged, so `ALLOWED_SENDERS` is a convenience
filter, not a control. SES evaluates SPF, DKIM, DMARC, spam and virus before
invoking the function and passes the verdicts in the event; the handler
enforces them.

Mail flagged as spam or carrying a virus is always dropped. `REQUIRE_AUTH`
controls the rest — `any` (default) accepts a pass on any of SPF, DKIM or DMARC,
`dmarc` demands a DMARC pass specifically, and `none` skips the check.

`dmarc` is the strictest but rejects senders that publish no DMARC record, which
SES reports as `GRAY` rather than a failure. Every invocation logs the five
verdicts, so check what your sender actually produces before tightening.

## Setup

See [SETUP.md](SETUP.md) — a step-by-step console walkthrough, no CLI required.
Roughly: verify a domain in SES, add an MX record, create a bucket, deploy the
function, create a bot on Slack, Discord or both, write the routing parameter,
and add a receipt rule. The AWS half is the same for either platform.

Deploy `ses_to_chat.py`. 

### Environment variables

| Variable | Purpose |
|---|---|
| `BUCKET` | S3 bucket the SES rule writes to |
| `PREFIX` | key prefix from the S3 action (default `inbound/`) |
| `ROUTES_PARAM` | SSM SecureString holding the routing JSON |
| `ROUTES_JSON` | inline routing JSON; used only if `ROUTES_PARAM` is unset |
| `ROUTES_CACHE_TTL` | seconds to cache the routing table (default 300) |
| `ALLOWED_SENDERS` | comma-separated; default `alerts@boondockecho.com` |
| `REQUIRE_AUTH` | `any` (default), `dmarc`, or `none` — strictness of the SPF/DKIM/DMARC check |
| `DISPLAY_TZ` | IANA zone for the timestamp (default `UTC`) |
| `DATETIME_FORMAT` | strftime pattern for the header time (default `— _%d %b %Y %H:%M_`; `""` omits it) |

Standard library plus boto3 only — no packaging step, paste it into the console
editor.

## Not for emergencies

This is a best-effort relay of a best-effort transcription. Audio gets missed,
Whisper mishears, keyword matching is literal, and email delivery is neither
guaranteed nor timely. Do not rely on it for emergency notification or anywhere
a missed or delayed message matters. It is not a substitute for monitoring the
radio.

## License

MIT — see [LICENSE](LICENSE). Provided as-is, without warranty of any kind,
express or implied.
