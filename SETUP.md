# Boondock Echo → SES → Slack or Discord — console walkthrough (us-west-2)

Target: mail to an address on your domain lands in S3, triggers a Lambda, and
posts a formatted message with the recording attached. No CLI required.

The AWS half is identical whichever chat platform you use. Only **step 4** (the
bot) and the `platform` field in **step 6** (routing) differ. You can do one
platform, or both — a single Lambda serves any mix.

**Before anything:** set the region picker in the top-right of the console to
**US West (Oregon) us-west-2** and keep it there for every step. Resources
created in the wrong region won't see each other, and SES email receiving isn't
offered in most regions at all.

Grab your 12-digit account ID now — click your account name in the top-right,
it's listed there. You'll need it once.

**On the names below:** `boondock-inbound`, `boondock-to-chat` and
`boondock-relay` are arbitrary labels, but the first two appear in both the
bucket policy and the receipt rule and **must match between them**. If you
already have these resources under other names, keep yours and substitute
throughout.

---

## 1. Verify the domain in SES

**SES console → Configuration → Identities → Create identity**

- Identity type: **Domain**
- Domain: `subdomain.dev.null`
- Leave "Assign a default configuration set" and "custom MAIL FROM" unchecked
- Under Verifying your domain, leave **Easy DKIM** with 2048-bit selected
- Create identity

SES shows you **three CNAME records**. If Route 53 hosts `dev.null`, there's a
**Publish DNS records to Route 53** button — click it and you're done. Otherwise
copy all three into your DNS provider.

Watch for the trailing-domain trap: if your DNS UI appends the zone name
automatically, enter the host portion only (`abc123._domainkey.subdomain`), not
the full name, or you'll end up with `…subdomain.dev.null.dev.null`.

The identity page will show **Verified** once DNS propagates — usually 15–60
minutes, occasionally a few hours.

## 2. Add the MX record

At your DNS provider, on `subdomain.dev.null`:

| Type | Name | Priority | Value |
|---|---|---|---|
| MX | `subdomain.dev.null` | 10 | `inbound-smtp.us-west-2.amazonaws.com` |

This routes mail for the `subdomain.` label only. Anything addressed to
`dev.null` itself is untouched.

While DNS settles, do steps 3 through 8 — none of them depend on it.

---

## 3. Create the S3 bucket

**S3 console → Create bucket**

- Region: **US West (Oregon) us-west-2**
- Name: something globally unique, e.g. `boondock-inbound-mail-<your-account-id>`
- Leave **Block all public access** checked
- Create bucket

### Lifecycle rule (so raw MIME doesn't accumulate forever)

Open the bucket → **Management** tab → **Create lifecycle rule**

- Name: `expire-inbound`
- Scope: **Limit the scope using prefixes** → prefix `inbound/`
- Check **Expire current versions of objects** → **30** days after creation
- Create rule

### Bucket policy

Open the bucket → **Permissions** tab → **Bucket policy** → **Edit** → paste,
replacing `BUCKET-NAME` and `ACCOUNT-ID`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowSESPuts",
      "Effect": "Allow",
      "Principal": { "Service": "ses.amazonaws.com" },
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::BUCKET-NAME/*",
      "Condition": {
        "StringEquals": {
          "AWS:SourceAccount": "ACCOUNT-ID",
          "AWS:SourceArn": "arn:aws:ses:us-west-2:ACCOUNT-ID:receipt-rule-set/boondock-inbound:receipt-rule/boondock-to-chat"
        }
      }
    }
  ]
}
```

That SourceArn references a rule set and rule that don't exist yet. That's fine —
but in step 8 you **must** name them exactly `boondock-inbound` and
`boondock-to-chat`. A mismatch means SES accepts the mail and then silently
discards it, with no bounce and no error anywhere.

If the SES console later offers to update the bucket policy for you when you
pick the bucket, let it — it writes the same thing with the names filled in.

---

## 4. Create the bot

Do **4a** for Slack, **4b** for Discord, or both. Either way you end up with two
values per destination: a **token** and a **channel ID**.

Neither platform accepts a channel *name* — the file-upload APIs require the ID,
and the Lambda rejects a `#name` at load rather than failing mid-post.

### 4a. Slack

A webhook can only post text. Attaching the audio needs a bot token, so use
that instead.

1. Go to **api.slack.com/apps** → **Create New App** → **From scratch**
2. Name it `Boondock Echo`, pick your workspace
3. Left sidebar → **OAuth & Permissions**
4. Under **Scopes → Bot Token Scopes**, add both:
   - `chat:write`
   - `files:write`
5. Scroll up → **Install to Workspace** → Allow
6. Copy the **Bot User OAuth Token** — starts with `xoxb-`

Then, in Slack, invite the bot to the target channel:

```
/invite @Boondock Echo
```

It can't post to a channel it isn't in, and the error otherwise
(`not_in_channel`) only shows up in CloudWatch.

**Channel ID:** open the channel, click its name at the top, scroll to the
bottom of the About tab. It looks like `C09ABCDEFGH`.

While you're there, **Basic Information → Display Information** is where the bot
icon and description go. Optional, but it's what people see on every message.

### 4b. Discord

1. Go to **discord.com/developers/applications**, signed in to Discord in that
   browser → **Create App** (older guides call it *New Application*)
2. Name it `Boondock Echo` → **Create**
3. Left sidebar → **Bot** → **Reset Token** → copy it (shown once)
4. Left sidebar → **Installation** → **Default Install Settings** → Guild
   Install:
   - Scopes: **bot**
   - Permissions: **Send Messages** and **Attach Files**
5. Copy the **Install Link**, open it, choose **Add to server**, pick your
   server, authorise

The **OAuth2 → URL Generator** page produces the same kind of invite link if you
prefer it.
6. In Discord, make sure the bot's role can see and post in the target channel —
   channel-level overrides beat server-level permissions

**Channel ID:** Discord **Settings → Advanced → Developer Mode** on, then
right-click the channel → **Copy Channel ID**. It's 17–20 digits, e.g.
`1234567890123456789`.

Discord's per-file cap is far lower than Slack's and varies with server boost
level. Dispatch clips run ~100 KB so it rarely comes up, but long recordings
would hit it.

---

## 5. Create the Lambda function

**Lambda console → Create function**

- **Author from scratch**
- Function name: `boondock-relay`
- Runtime: **Python 3.12**
- Under **Change default execution role**, leave **Create a new role with basic
  Lambda permissions**
- Create function

### Paste the code

In the **Code** tab, the editor opens `lambda_function.py`. Select all, delete,
and paste the entire contents of `ses_to_chat.py`. Click **Deploy**.

Leave the handler as the default `lambda_function.lambda_handler` — since you
pasted into `lambda_function.py`, that matches. (Ignore the source filename; it
only matters for the zip-upload route.)

The code uses nothing outside the standard library and boto3, so there's no
packaging step.

### Environment variables

**Configuration** tab → **Environment variables** → **Edit** → add:

| Key | Required | Value |
|---|---|---|
| `BUCKET` | yes | your bucket name |
| `PREFIX` | no | key prefix from the S3 action; defaults to `inbound/` |
| `ROUTES_PARAM` | yes* | `/boondock/routes` — the SSM parameter from step 6 |
| `DISPLAY_TZ` | no | IANA zone for the timestamp, e.g. `America/Los_Angeles`; defaults to `UTC` |
| `DATETIME_FORMAT` | no | how the time appears on the header line; defaults to `— _%d %b %Y %H:%M_` — see below |
| `REQUIRE_AUTH` | no | `any` (default), `dmarc`, or `none` — see step 9 |
| `ALLOWED_SENDERS` | no | comma-separated; defaults to `alerts@boondockecho.com` |
| `ROUTES_CACHE_TTL` | no | seconds to cache the routing table; defaults to `300` |

`DATETIME_FORMAT` uses Python's
[strftime codes](https://docs.python.org/3/library/datetime.html#format-codes),
and anything else in it passes through as written — separators, emoji
shortcodes, and markup alike. The default's leading `—` keeps the time apart
from whatever comes before it, keyword or not, and `_…_` renders it in italics
on both Slack and Discord:

```
🚨 Fire Dispatch [vegetation fire] — 05 Sep 2026 18:33
🚨 Fire Dispatch — 05 Sep 2026 18:33
```

`%H:%M` gives just the time; an empty value leaves the time off entirely.

\* To skip SSM while testing, leave `ROUTES_PARAM` unset and set
`SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` instead — the code falls back to a
single catch-all route. That fallback is Slack-only; Discord needs the routing
table.

`ALLOWED_SENDERS` already defaults to Boondock's sending address, so an address
published in your DNS can't be used by anyone else to post into your channels.

### Timeout and memory

**Configuration** → **General configuration** → **Edit**

- Timeout: **1 min** (the default 3 seconds won't survive an S3 read plus a file
  upload, and fan-out to several channels is several uploads)
- Memory: **512 MB** (clips are held in memory; raise it if your recordings are
  long)
- Save

### Let the function read the bucket and the routing parameter

**Configuration** → **Permissions** → click the **Role name** link. That opens
IAM in a new tab.

**Add permissions** → **Create inline policy** → **JSON** tab → paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::BUCKET-NAME/inbound/*"
    },
    {
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:us-west-2:ACCOUNT-ID:parameter/boondock/routes"
    },
    {
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "*"
    }
  ]
}
```

Next → name it `boondock-relay` → Create policy.

The `kms:Decrypt` entry is what lets the function read a SecureString. You can
scope its `Resource` to the ARN of the `alias/aws/ssm` key if you'd rather not
leave it as `*`.

---

## 6. Create the routing parameter

This one parameter holds every destination — tokens, channel IDs, and the rules
for which alerts go where.

**Systems Manager console → Parameter Store → Create parameter**

- Name: `/boondock/routes`
- Tier: **Standard**
- Type: **SecureString**
- KMS key source: **My current account**, key `alias/aws/ssm`
- Value: one of the JSON documents below

**Simplest possible start** — everything to one channel:

```json
{
  "dispatch": {
    "_channel": "#alerts",
    "token": "xoxb-YOUR-TOKEN",
    "channel": "C0YOURCHANNEL",
    "always": true
  }
}
```

**Discord** — identical but for `platform` and the ID format:

```json
{
  "dispatch": {
    "_channel": "#alerts",
    "platform": "discord",
    "token": "YOUR-BOT-TOKEN",
    "channel": "1234567890123456789",
    "always": true
  }
}
```

**A realistic table** — filtered channels alongside a catch-all, across both
platforms:

```json
{
  "fires": {
    "_channel": "#fires",
    "token": "xoxb-YOUR-TOKEN",
    "channel": "C0AAAAAAAAA",
    "label": "Fire Dispatch",
    "emoji": ":fire_engine:",
    "sources": ["Fire Dispatch"],
    "keywords": ["structure fire", "vegetation fire"]
  },
  "everything": {
    "_channel": "#dispatch-all",
    "token": "xoxb-YOUR-TOKEN",
    "channel": "C0BBBBBBBBB",
    "emoji": ":radio:",
    "always": true
  },
  "mutual-aid": {
    "_channel": "#aid (other org)",
    "platform": "discord",
    "token": "YOUR-BOT-TOKEN",
    "channel": "1234567890123456789",
    "emoji": ":handshake:",
    "keywords": ["structure fire"]
  }
}
```

### How a route matches

The route's **key** is a name only — it never matches anything. Matching is
explicit, through these fields:

| Field | Matches |
|---|---|
| `always: true` | everything — a catch-all channel |
| `addresses` | the recipient, by full address or local part |
| `keywords` | substring of the alert's `Keywords Matched`, case-insensitive |
| `sources` | the source name from the body, exact, case-insensitive |
| `transcript` | whole-word match in the transcript itself |

**Every field a route specifies must match**, and fields it omits are ignored.
So `sources` plus `keywords` means *this scanner AND one of these keywords* — a
route for one node's fire traffic won't also pick up that node's medical calls.

**Every matching route receives the alert**, not just the first, so a catch-all
can sit alongside filtered channels. Routes sharing a token and channel are
collapsed, so overlapping rules post once.

`default` is the one reserved key: it fires only when nothing else matched, so
it stays a fallback rather than a second catch-all. Without a `default`, an
unmatched alert is dropped — which is usually what you want, but it means a
typo'd `sources` value fails silently. A `default` pointed at a low-traffic
channel makes misconfiguration visible instead.

`keywords` trusts the device's own matcher. `transcript` searches what was
actually said, so filtering can live here instead of in the Boondock console —
set the device to send everything and decide in this table.

Other fields: `platform` is `slack` (default) or `discord`. `label` overrides
the source name shown in the message and `emoji` is a shortcode prefixed to it
(default `:rotating_light:`); both are display-only and never affect matching.
Keys starting with `_` are ignored, so `"_channel": "#fires"` works as a
comment — worth having, since a bare channel ID tells you nothing later.

A route with none of the five matchers can never fire; the function logs a
`WARNING` naming it at load.

### Editing it later

The function caches this for 5 minutes, so edits take effect on their own with
no redeploy. Standard-tier parameters are free and cap at 4 KB of value, roughly
30 routes, and **Parameter Store keeps a version history** — if a change breaks
something, the previous value is on the parameter's History tab. Rolling back is
copy-and-save, not a revert button.

AWS validates almost nothing here: malformed JSON, a bad channel ID, a missing
`channel` key all save cleanly and fail at the next alert. And the failure is
total rather than per-route — the whole document is parsed and every channel ID
validated before anything is selected, so one bad entry drops *all* alerts. Run
the JSON through a validator before saving, then replay a test event (below) and
confirm the log shows `loaded N route(s)` with the count you expect.

---

## 7. Let SES invoke the Lambda

The SES console usually adds this permission for you when you attach the Lambda
action in step 8. Do step 8 first, then come back and verify here.

**Lambda → your function → Configuration → Permissions**, scroll to
**Resource-based policy statements**. You want a statement with principal
`ses.amazonaws.com`.

If it's missing: **Add permissions** → **AWS service** → Service: **Other**

- Statement ID: `ses-invoke`
- Principal: `ses.amazonaws.com`
- Source account: your account ID
- Action: `lambda:InvokeFunction`
- Save

---

## 8. Create the receipt rule

**SES console → Email receiving**

First check whether a rule set is already active — only one can be active per
region. If there's an existing active set, add your rule to it rather than
creating a new one, and fix the SourceArn in the bucket policy to match that
set's name.

**Create rule set** → name `boondock-inbound` → Create.

Open it → **Create rule**:

**Step 1 — Rule settings**
- Rule name: `boondock-to-chat`
- Status: **Enabled**
- Transport Layer Security: **Optional**
- Spam and virus scanning: **Enabled**

**Step 2 — Add recipient conditions**
- Add new recipient condition → `subdomain.dev.null`

Enter the **domain**, not a single address. That makes the rule a catch-all, so
adding a scanner later means adding a routing entry and pointing the device at a
new address — no AWS change at all. The Lambda decides where each message goes
and drops anything unrouted.

**Step 3 — Add actions** (order matters — S3 first)
- Add new action → **Deliver to Amazon S3 bucket**
  - S3 bucket: your bucket
  - Object key prefix: `inbound/`
  - Leave encryption and SNS topic unset
  - If prompted to update the bucket policy, accept
- Add new action → **Invoke AWS Lambda function**
  - Function: `boondock-relay`
  - Invocation type: **Event**
  - If prompted to add the Lambda permission, accept

S3 must come before Lambda. SES hands a Lambda only the headers, never the
body — the function reads the message back out of the bucket, so the object has
to exist by the time it runs.

**Step 4** — review → Create rule.

Finally, back on **Rule sets**, select `boondock-inbound` and click **Set as
active**. This is easy to miss and nothing works without it.

---

## 9. Test

Check the identity shows **Verified** in SES → Identities first.

Send a plain email from any account to an address on the domain, e.g.
`firedispatch@subdomain.dev.null`. A plain message with no Boondock fields still
exercises the whole path — it just posts with an empty transcript.

- **S3 console** → your bucket → `inbound/` — an object should appear within
  seconds, named with a long message ID.
- **CloudWatch console** → **Logs** → **Log groups** →
  `/aws/lambda/boondock-relay` → newest log stream.
- Your channel should have the message.

A successful run logs roughly this:

```
loaded 1 route(s): ['dispatch']
verdicts: {'spf': 'PASS', 'dkim': 'PASS', 'dmarc': 'PASS', 'spam': 'PASS', 'virus': 'PASS'}
to=['firedispatch@subdomain.dev.null'] source=None trigger=None clips=[] targets=['dispatch (always)'] recorded=unknown
posted to C0YOURCHANNEL via dispatch (always)
```

With a real alert, `clips` names the mp3 and `targets` lists every destination
that matched, each with the reason:

```
to=['firedispatch@subdomain.dev.null'] source='Fire Dispatch' trigger='vegetation fire' clips=['21-19-22.mp3'] targets=["fires (keyword 'vegetation fire' + source 'Fire Dispatch')", 'everything (always)'] recorded=2026-09-06T21:19:22+00:00
staged 21-19-22.mp3 (113877 bytes)
posted to C0AAAAAAAAA via fires (keyword 'vegetation fire' + source 'Fire Dispatch') (audio=True)
posted to C0BBBBBBBBB via everything (always) (audio=True)
```

That `targets=[...]` line is the one to read when something lands in the wrong
place — it names every rule that fired and why.

`recorded=` is when the recording ended, read from Boondock's audio filename.
That's the time shown on the message. It's `unknown` for a plain test email with
no Boondock audio, in which case the message falls back to the email's own send
time.

### A note on `REQUIRE_AUTH`

SES evaluates SPF, DKIM, DMARC, spam and virus before invoking the function and
passes the verdicts in the event; the handler enforces them, because the
envelope sender is trivially forged and `ALLOWED_SENDERS` alone is not a
control. Spam and virus failures are always dropped.

`any` (the default) accepts a pass on any of SPF, DKIM or DMARC. `dmarc` demands
a DMARC pass specifically — stricter, but it rejects senders that publish no
DMARC record, which SES reports as `GRAY` rather than a failure. The `verdicts:`
log line shows what your sender actually produces, so check that before
tightening.

Then point the Boondock Echo's alert email destination at the address and
trigger a keyword.

---

## Replaying a message by hand

You can invoke the function against an object already in S3, without waiting for
a real alert. Lambda console → your function → **Test** → **Create new event**,
and paste:

```json
{
  "Records": [
    {
      "eventSource": "aws:ses",
      "ses": {
        "mail": {
          "messageId": "PASTE-THE-S3-OBJECT-NAME",
          "source": "alerts@boondockecho.com",
          "destination": ["firedispatch@subdomain.dev.null"]
        },
        "receipt": {
          "recipients": ["firedispatch@subdomain.dev.null"],
          "spfVerdict":   {"status": "PASS"},
          "dkimVerdict":  {"status": "PASS"},
          "dmarcVerdict": {"status": "PASS"},
          "spamVerdict":  {"status": "PASS"},
          "virusVerdict": {"status": "PASS"}
        }
      }
    }
  ]
}
```

Everything else — transcript, keyword, timestamp, the mp3 — comes from the S3
object itself. Three things must line up:

- `messageId` equals the S3 object name, minus the `inbound/` prefix
- `source` matches `ALLOWED_SENDERS`, or the run drops immediately
- the verdict blocks are present, or the authentication check drops it

Real SES events carry those verdicts automatically. A hand-written event has to
include them, which is why they're in the sample above.

This posts for real, so aim at a scratch channel while you tune. Save a couple
of different messages as separate test events — a long transcript and a short
one — and you can check formatting without waiting for live traffic.

Note the console has no staging slot: **Deploy is publishing**, and the Test
button runs whatever is currently deployed. To try changes without touching a
live feed, clone the function, point its `ROUTES_PARAM` at a second parameter
holding scratch channels, and replay against that.

---

## Adding a scanner, a channel, or a platform later

**Another channel, same workspace or server:** add a routing entry with the same
token and the new channel ID, invite the bot to it, and give it `keywords`,
`sources`, `transcript` or `always`. No AWS change; the parameter is re-read
within 5 minutes.

**Another scanner:** point the device's alert email at a new address, e.g.
`police@subdomain.dev.null`, and add `"addresses": ["police@subdomain.dev.null"]`
to the routes that should receive it. The catch-all receipt rule already accepts
the address. If one device feeds everything, use `sources` or `keywords` instead
— there's no need for separate addresses at all.

**Another workspace or server:** repeat step 4 there — Slack apps are
per-workspace and Discord bots are per-application — then add a routing entry
with the new token. Different destinations are just different tokens in the same
table; one Lambda serves all of them.

**Another platform:** add `"platform": "discord"` to a route and give it that
platform's token and channel ID. A single alert can fan out to Slack and Discord
at once.

**Migrating a channel:** give the old and new entries both `"always": true` (or
the same filters), confirm the new one looks right, then delete the old entry.
Nothing goes dark in between.

---

## Troubleshooting

### Mail and AWS

**Mail bounces or never arrives.** MX record wrong or not propagated, or the
domain still isn't verified. Both are visible in SES → Identities and by
querying MX for the domain at mxtoolbox.com.

**Nothing in S3.** Almost always the bucket policy SourceArn not matching the
real rule set and rule names, character for character. SES accepts the mail and
discards it silently. To isolate, temporarily delete the whole `"Condition"`
block from the bucket policy, test, then restore it. Also confirm the rule set
is the *active* one.

**Object in S3 but no CloudWatch log group.** SES isn't allowed to invoke the
function — go back to step 7.

**Log group exists but shows `AccessDenied` on GetObject.** The inline IAM policy
in step 5 is missing, or its resource path doesn't match the `inbound/` prefix.

**`NoSuchKey` in the logs.** The `PREFIX` environment variable and the rule's
object key prefix disagree. Both should be `inbound/`, trailing slash included.

**`ParameterNotFound`.** The SSM parameter name doesn't match `ROUTES_PARAM`
exactly — check the leading slash, and check the Parameter Store console is in
us-west-2. Parameters are regional.

**`AccessDeniedException` on `ssm:GetParameter`, or a KMS error.** The inline
policy is missing the `ssm:GetParameter` or `kms:Decrypt` statement.

### Routing

**Alert posted to the wrong place, or nowhere.** Read the `targets=[...]` line —
it names every rule that fired and why. If it's empty, nothing matched and there
was no `default`.

**A route never fires.** If the log shows `WARNING route 'x' has none of
('always', 'addresses', 'keywords', 'sources', 'transcript')`, that route has no
matcher — the key is a name, not a rule. Otherwise check the AND semantics: a
route with both `sources` and `keywords` needs both to match.

**Every alert dropped after a parameter edit.** One bad entry fails the whole
table. Check the log for a `ValueError` naming the route, and roll back from the
parameter's History tab.

**A `keywords` route goes quiet.** If the device is configured with a wildcard
(`*`), it may stop reporting real keyword matches. The relay strips a bare `*`
from the reported keyword, so a wildcard-only alert has no keyword to match on —
use `transcript` instead, which searches what was actually said.

### Slack

**`not_in_channel`.** The bot isn't a member. Run `/invite @Boondock Echo` in
the channel.

**`missing_scope`.** The log names what was needed and what the token carries.
Add the scope under OAuth & Permissions, then **reinstall** the app — scope
changes don't apply until you do.

**`invalid_arguments` mentioning `channel_id`.** A channel *name* where an ID
belongs. `chat.postMessage` tolerates `#name` but file uploads don't. The
function now rejects this at load with a message naming the route.

**`504` or a timeout on the audio upload.** Slack's gateway timed out; the
request reached them and their backend didn't answer. Transient, and nothing to
tune locally. Each step retries twice with backoff (1s, then 2s), and the log
names the step:

```
slack file upload: HTTP 504 — retrying in 1.0s (attempt 1/3)
```

Which step matters. `slack files.getUploadURLExternal` and `slack file upload`
are safe to retry — a failed transfer gets a fresh upload slot, and an abandoned
slot never becomes visible. `slack files.completeUploadExternal` is the one that
makes the message appear, so a 504 there is **ambiguous**: Slack may have posted
it and only failed to say so. It's retried anyway, because losing the alert is
worse than a duplicate, and a success after that logs:

```
...succeeded after an ambiguous failure — a duplicate post is possible
```

If all attempts fail, the text still posts, with Boondock's own recording link
appended (valid about 30 days). The note under it tells you which case you hit:
*audio upload failed at …* means the clip definitely didn't post; *the version
with audio may still appear* means the finalising step was ambiguous. The raw
email is in S3 for 30 days, so either way you can replay it to re-attempt.

A retry is skipped if the invocation doesn't have time left to finish it, which
logs `not retrying, only Ns left`. If you see that often, raise the Lambda
timeout.

### Discord

Failures log as `discord create-message: HTTP <code> <body>`, inside a
`FAILED posting to …` line if even the text fallback failed.

**`HTTP 401`.** The token is wrong, or was reset in the Developer Portal (which
invalidates the old one). Note the auth scheme is the literal word `Bot`, not
`Bearer` — the code handles that, but it's the usual cause if you've adapted it
elsewhere.

**`HTTP 403`.** The bot is in the server but can't post in that channel. Check
channel-level permission overrides, not just the role's server-wide
permissions. It needs **Send Messages** and, for the clip, **Attach Files**.

**`HTTP 404`.** Wrong channel ID, or the bot isn't in that server at all.

**`HTTP 429 … retrying in Ns`.** Rate limited. The sender waits the interval
Discord reports and retries. Frequent ones mean you're fanning out to more
Discord channels than the rate limit allows in a burst.

**`HTTP 5xx`.** Retried like Slack's. Discord posts in one request, so a 5xx is
always ambiguous — the message may exist even though the call failed.

**`HTTP 413`, or a payload-size complaint.** Discord's per-file cap is much
lower than Slack's and depends on server boost level. Long recordings will
exceed it; dispatch clips won't.

---

## Notes

- The SES sandbox limits **sending**, not receiving. You don't need production
  access for this to work.
- Cost is effectively nothing at low volume — SES inbound runs about $0.10 per
  1,000 emails, and Lambda plus S3 stay inside the free tier. At ~8,000 alerts a
  month it's still under $2. The line item that grows quietly is CloudWatch
  Logs: a Lambda-created log group defaults to **Never expire**, so set a
  retention of 30 days on it (CloudWatch → Logs → the log group → Actions →
  Edit retention).
- To change the code later: Lambda console → Code tab → edit → **Deploy**.
  Nothing else needs redeploying.
- This is a best-effort relay of a best-effort transcription. Don't rely on it
  for emergency notification — see the README.
