# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Jeremy Dean
"""
SES inbound -> S3 -> Lambda -> Slack or Discord (multi-scanner, multi-server)

Each alert becomes ONE message, on Slack or Discord:

    :fire_engine: *Fire Dispatch* `[engine]` — _01 Oct 2026 16:26_
    > engine 46 medic 69 medical 123 Fake Street
    [audio player]

Source in bold, matched keyword in inline code, the time the recording ended
(per DATETIME_FORMAT, in DISPLAY_TZ), transcript as a blockquote, mp3 attached
below. No thread, no
metadata row.

Triggered by an SES receipt rule whose actions are, in order:
  1. S3 action     (bucket = BUCKET, prefix = PREFIX)
  2. Lambda action (Event invocation, this function)

Set the rule's recipient condition to the whole domain (subdomain.dev.null) so
adding a scanner needs no AWS change — just a new routing entry and a new
address on the device.

EVERY matching route receives the alert, not just the first. A route's key is
a name only — matching is explicit, via any of these fields:

  "always":    true                       catch-all
  "addresses": ["fire@subdomain.dev.null"] recipient, full address or local part
  "keywords":  ["vegetation fire"]        substring of "Keywords Matched"
  "sources":   ["Fire Dispatch"]          exact source name from the body
  "transcript":["vegetation fire"]        whole-word match in the transcript

Each route also takes "platform": "slack" (default) or "discord". The two
differ only in the sender: Slack wants a bot token with chat:write and
files:write and a C… channel ID; Discord wants a bot token with Send Messages
and Attach Files and a numeric channel ID (Developer Mode -> Copy Channel ID).
A single alert can fan out to both.

EVERY field a route specifies must match — "sources" plus "keywords" means
this source AND one of these keywords. Omitted fields are ignored.

"default" is the reserved exception — it fires only when nothing else matched,
so it stays a fallback. Routes sharing a token and channel are collapsed, so
overlapping rules post once. Nothing matching and no default means the alert is
dropped rather than posted somewhere arbitrary.

Environment variables:
  BUCKET             S3 bucket the SES rule writes to
  PREFIX             key prefix used by the S3 action (default "inbound/")
  ROUTES_PARAM       SSM parameter (SecureString) holding the routing JSON
  ROUTES_JSON        inline routing JSON; used only if ROUTES_PARAM is unset
  ROUTES_CACHE_TTL   seconds to cache the routing table (default 300)
  ALLOWED_SENDERS    comma-separated; default alerts@boondockecho.com
  REQUIRE_AUTH       "any" (default), "dmarc" or "none" — how strictly to
                     enforce the SPF/DKIM/DMARC verdicts SES supplies
  DISPLAY_TZ         IANA zone for the timestamp, e.g. America/Los_Angeles
                     (default UTC)
  DATETIME_FORMAT    strftime pattern for the time on the header line
                     (default "— _%d %b %Y %H:%M_"). Literal text and markup
                     pass through: the leading "—" separates the time from
                     whatever precedes it, keyword or not, and "_..._" is
                     italic on both platforms. Set to "" to omit the time.
                     Codes: https://docs.python.org/3/library/datetime.html#format-codes

Routing JSON shape — keys are lowercased on load:

  {
    "fire":       {"token": "xoxb-...", "channel": "C0AAA",
                   "addresses": ["fire@subdomain.dev.null"],
                   "emoji": ":fire_engine:"},
    "sheriff":    {"token": "xoxb-...", "channel": "C0BBB",
                   "sources": ["My County Sheriff"],
                   "label": "My County Sheriff"},
    "wildland":   {"token": "xoxb-...", "channel": "C0CCC",
                   "keywords": ["vegetation fire"],
                   "emoji": ":evergreen_tree:"},
    "everything": {"token": "xoxb-...", "channel": "C0DDD",
                   "always": True},
    "default":               {"token": "xoxb-...", "channel": "C0ZZZ"}
  }

"label" overrides the source name the device reports; "emoji" is a shortcode
prefixed to the message, defaulting to :rotating_light:. Standard shortcodes
render on both platforms. Both are
display-only and never affect matching. Keys beginning with "_" are ignored,
so "_channel": "#fire-dispatch" is a usable comment.

Keyword matching is substring and case-insensitive; source matching is exact.
Since every match fires, overlapping keywords fan out rather than competing —
a "fire" route and a "vegetation fire" route both receive a vegetation fire
alert.

Entries pointing at different workspaces or servers simply carry different
tokens. Each Slack workspace needs its own app with chat:write and files:write;
each Discord server needs the bot invited with Send Messages and Attach Files.
Either way, the bot must be able to post in the target channel.

Runtime: python3.12+. Standard library plus boto3 only.

Provided as-is under the MIT License, without warranty of any kind, express or
implied. This is a best-effort relay of a best-effort transcription: audio can
be missed, keywords can fail to match, Whisper mishears, and email delivery is
not guaranteed or timely. Do not rely on it for emergency notification or for
any purpose where a missed or delayed message matters. It is not a substitute
for monitoring the radio.
"""

import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import boto3

s3 = boto3.client("s3")
ssm = boto3.client("ssm")

BUCKET = os.environ["BUCKET"]
PREFIX = os.environ.get("PREFIX", "inbound/")
ROUTES_PARAM = os.environ.get("ROUTES_PARAM")
ROUTES_JSON = os.environ.get("ROUTES_JSON")
CACHE_TTL = int(os.environ.get("ROUTES_CACHE_TTL", "300"))
ALLOWED = [
    s.strip().lower()
    for s in os.environ.get("ALLOWED_SENDERS", "alerts@boondockecho.com").split(",")
    if s.strip()
]

# Per-message text limits. Discord rejects content over 2,000 characters
# outright (HTTP 400, not retried), so exceeding it would lose the alert.
MAX_TEXT = {"slack": 2900, "discord": 2000}

# Bold differs: Slack mrkdwn uses *x*, Discord markdown uses **x** (a single
# asterisk is italic there). Inline code, _italic_ and > quotes are shared.
BOLD = {"slack": "*", "discord": "**"}

DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "UTC")
try:
    _TZ = ZoneInfo(DISPLAY_TZ)
except Exception as exc:  # tzdata missing or a bad name
    print(f"timezone {DISPLAY_TZ!r} unavailable ({exc}) — falling back to UTC")
    _TZ = timezone.utc

DATETIME_FORMAT = os.environ.get("DATETIME_FORMAT", "— _%d %b %Y %H:%M_")

# Only consulted when the content type is unhelpful — some mailers label every
# attachment application/octet-stream. Audio types are matched by content type
# first, so this list only needs to cover formats a recorder might actually
# produce, not every audio format that exists.
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac"}


AUTH_MODE = os.environ.get("REQUIRE_AUTH", "any").strip().lower()

VERDICTS = ("spf", "dkim", "dmarc", "spam", "virus")


def check_receipt(receipt):
    """
    SES evaluates SPF, DKIM, DMARC, spam and virus before invoking us, and
    hands the results over in the event. ALLOWED_SENDERS alone is not a
    control — the envelope sender is trivially forged — so the verdicts are
    what actually stop someone posting into the channel by guessing an address.

    A status of GRAY means the sending domain publishes no such record, so
    requiring a strict DMARC pass can reject legitimate mail from a sender
    with no DMARC policy. REQUIRE_AUTH picks the strictness:

        any    (default) at least one of SPF / DKIM / DMARC must PASS
        dmarc  DMARC specifically must PASS
        none   skip authentication checks (spam and virus still enforced)

    Returns (reason_to_drop_or_None, verdicts_dict).
    """
    seen = {
        name: str((receipt.get(f"{name}Verdict") or {}).get("status", "ABSENT")).upper()
        for name in VERDICTS
    }

    if seen["virus"] == "FAIL":
        return "virus detected", seen
    if seen["spam"] == "FAIL":
        return "flagged as spam", seen

    if AUTH_MODE == "none":
        return None, seen

    if AUTH_MODE == "dmarc":
        if seen["dmarc"] != "PASS":
            return f"dmarc={seen['dmarc']} (REQUIRE_AUTH=dmarc)", seen
        return None, seen

    if any(seen[n] == "PASS" for n in ("dmarc", "spf", "dkim")):
        return None, seen
    return "no SPF, DKIM or DMARC pass", seen


# --------------------------------------------------------------------------
# routing table
# --------------------------------------------------------------------------

_cache = {"routes": None, "at": 0.0}

SLACK_CHANNEL_RE = re.compile(r"^[CGDZ][A-Z0-9]{8,}$")
DISCORD_CHANNEL_RE = re.compile(r"^\d{17,20}$")   # snowflake

PLATFORMS = ("slack", "discord")

MATCHERS = ("always", "addresses", "keywords", "sources", "transcript")


def _normalize(key, route):
    """Channel IDs look different per platform, and neither accepts a #name."""
    platform = str(route.get("platform", "slack")).strip().lower()
    if platform not in PLATFORMS:
        raise ValueError(f"route {key!r}: platform {platform!r} is not one of {PLATFORMS}")

    raw = str(route.get("channel", "")).strip().lstrip("#")

    if platform == "discord":
        ch = raw
        if not DISCORD_CHANNEL_RE.match(ch):
            raise ValueError(
                f"route {key!r}: channel {route.get('channel')!r} is not a Discord "
                "channel ID. Enable Developer Mode in Discord settings, right-click "
                "the channel and Copy Channel ID (17-20 digits)."
            )
    else:
        ch = raw.upper()
        if not SLACK_CHANNEL_RE.match(ch):
            raise ValueError(
                f"route {key!r}: channel {route.get('channel')!r} is not a Slack "
                "channel ID. Open the channel in Slack, click its name, and copy the "
                "Channel ID from the bottom of the About tab (looks like C09ABCDEFGH)."
            )

    # The key is a name, not a matcher, so a route with no matcher can never
    # fire. Almost always a mistake — say so rather than failing silently.
    if key != "default" and not any(route.get(m) for m in MATCHERS):
        print(f"WARNING route {key!r} has none of {MATCHERS} — it will never match")

    return {**route, "channel": ch, "platform": platform}


def load_routes():
    """Cached across warm invocations so config edits apply without redeploy."""
    now = time.monotonic()
    if _cache["routes"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["routes"]

    if ROUTES_PARAM:
        raw = ssm.get_parameter(Name=ROUTES_PARAM, WithDecryption=True)["Parameter"]["Value"]
        routes = json.loads(raw)
    elif ROUTES_JSON:
        routes = json.loads(ROUTES_JSON)
    else:
        # single-workspace fallback, matching the original setup
        routes = {"default": {
            "token": os.environ["SLACK_BOT_TOKEN"],
            "channel": os.environ["SLACK_CHANNEL_ID"],
        }}

    routes = {str(k).strip().lower(): _normalize(k, v) for k, v in routes.items()}
    _cache.update(routes=routes, at=now)
    print(f"loaded {len(routes)} route(s): {sorted(routes)}")
    return routes


def route_matches(route, recipients, alert):
    """
    Does this route want this alert?

    EVERY field the route specifies must match; fields it omits are ignored.
    So "sources" and "keywords" together mean "this source AND one of these
    keywords" — which is the point of pairing them. A route listing only
    keywords matches on keyword alone, from any source.

        "always":     true                       catch-all, skips the rest
        "addresses":  ["fire@example.com"]       recipient, full or local part
        "keywords":   ["vegetation fire"]        substring of "Keywords Matched"
        "sources":    ["Fire Dispatch"]          exact source name from the body
        "transcript": ["vegetation fire"]        whole-word match in the transcript

    "keywords" trusts the device's own keyword matcher. "transcript" searches
    what was actually said, so filtering can live here instead of in the
    device — set the device to send everything and decide in this table.

    Transcript matching is whole-word, not substring: "sar" won't fire on
    "Caesar", but "fire" won't fire on "firefighter" either.

    Returns (description, matched_phrase) or (None, None).
    """
    if route.get("always"):
        return "always", None

    why, matched = [], None

    addrs = [str(a).strip().lower() for a in (route.get("addresses") or []) if str(a).strip()]
    if addrs:
        hit = None
        for r in recipients or []:
            addr = (r or "").strip().lower()
            if addr in addrs or addr.split("@")[0] in addrs:
                hit = addr
                break
        if not hit:
            return None, None
        why.append(f"address {hit}")

    keywords = [str(k).strip().lower() for k in (route.get("keywords") or []) if str(k).strip()]
    if keywords:
        hay = (alert.get("trigger") or "").lower()
        if not hay:
            return None, None
        hit = next((kw for kw in keywords if kw in hay), None)
        if not hit:
            return None, None
        why.append(f"keyword {hit!r}")
        matched = matched or hit

    phrases = [str(t).strip().lower() for t in (route.get("transcript") or []) if str(t).strip()]
    if phrases:
        hay = (alert.get("transcript") or "").lower()
        if not hay:
            return None, None
        hit = next((p for p in phrases if re.search(rf"\b{re.escape(p)}\b", hay)), None)
        if not hit:
            return None, None
        why.append(f"transcript {hit!r}")
        matched = matched or hit

    sources = [str(x).strip().lower() for x in (route.get("sources") or []) if str(x).strip()]
    if sources:
        src = (alert.get("source") or "").strip().lower()
        if not src or src not in sources:
            return None, None
        why.append(f"source {alert['source'].strip()!r}")

    # No matcher fields at all: never fires. _normalize warns at load time.
    if not why:
        return None, None

    return " + ".join(why), matched


def collect_routes(routes, recipients, alert):
    """
    Every matching route receives the alert, not just the first — so a
    catch-all channel can sit alongside filtered ones, and a message can go to
    more than one Slack workspace.

    "default" is the exception: it fires only when nothing else matched, so it
    stays a fallback rather than becoming a second catch-all. Duplicate
    (token, channel) pairs are collapsed, so overlapping rules post once.
    Order follows the routing table.
    """
    targets, seen = [], set()

    for key, route in routes.items():
        if key == "default":
            continue
        why, matched = route_matches(route, recipients, alert)
        if not why:
            continue
        ident = (route.get("token"), route.get("channel"))
        if ident in seen:
            print(f"route {key!r} matched ({why}) but duplicates an earlier target")
            continue
        seen.add(ident)
        targets.append((f"{key} ({why})", route, matched))

    if not targets and "default" in routes:
        targets.append(("default", routes["default"], None))

    return targets


# --------------------------------------------------------------------------
# email -> text
# --------------------------------------------------------------------------

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def extract_text(msg):
    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        return body.get_content().strip()

    body = msg.get_body(preferencelist=("html",))
    if body is not None:
        p = _Stripper()
        p.feed(body.get_content())
        return p.text()

    return ""


def extract_audio(msg):
    """Return [(filename, bytes), ...] for every audio part, inline or attached."""
    found = []

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue

        ctype = (part.get_content_type() or "").lower()
        fname = part.get_filename() or ""
        ext = os.path.splitext(fname)[1].lower()

        # video/mp4 is here because some encoders label .m4a audio that way.
        if not (ctype.startswith("audio/") or ctype == "video/mp4" or ext in AUDIO_EXTS):
            continue

        data = part.get_payload(decode=True)
        if not data:
            continue

        if not fname or not ext:
            ext = ext or mimetypes.guess_extension(ctype) or ".mp3"
            fname = f"audio-{uuid.uuid4().hex[:8]}{ext}"

        found.append((os.path.basename(fname), data))

    return found


# --------------------------------------------------------------------------
# parsing the Boondock Echo alert
# --------------------------------------------------------------------------
# Real format (2026-09):
#
#   Alert from Boondock Echo at Fire Dispatch
#   Transcription:  engine 46 medic 69 ...
#   Keywords Matched: engine
#   Audio File: https://...?Signature=...&Expires=...
#   Date and Time: Sep 04, 2026 - 01:47 PM PDT
#   Owner: Joe Blogs
#
# Anything unrecognised still lands in the transcript, so a format change
# degrades rather than breaks.

SOURCE_LINE_RE = re.compile(r"^\s*Alert from\s+.*?\s+at\s+(.+?)\s*$", re.I)
FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /_-]{2,30}?)\s*:\s*(.+?)\s*$")

# Longest / most specific labels first.
TRANSCRIPT_KEYS = ("transcription", "transcript", "text", "message")
SOURCE_KEYS = ("source", "channel", "talkgroup", "system", "frequency", "freq")
TRIGGER_KEYS = ("keywords matched", "keyword matched", "keywords", "keyword",
                "matched", "match", "alert", "trigger")
TIME_KEYS = ("date and time", "timestamp", "received", "time", "date")
AUDIO_URL_KEYS = ("audio file", "audio url", "recording", "audio")
IGNORE_KEYS = ("owner",)


def parse_alert(text):
    fields = {}
    leftovers = []
    source = None

    for line in text.splitlines():
        if not line.strip():
            continue

        if source is None:
            m = SOURCE_LINE_RE.match(line)
            if m:
                source = m.group(1).strip()
                continue

        m = FIELD_RE.match(line)
        if m:
            fields[m.group(1).strip().lower()] = m.group(2).strip()
        else:
            leftovers.append(line.strip())

    def pick(keys):
        for k in keys:
            if fields.get(k):
                return fields[k]
        return None

    def clean_trigger(value):
        """
        A wildcard keyword configured on the device ("*") makes it send every
        transmission. It's a routing instruction, not something that fired, so
        drop it — otherwise the message header reads `[*]`, and a route keyed
        on real keywords can't tell a wildcard-only alert from a real match.
        """
        if not value:
            return None
        parts = [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
        parts = [p for p in parts if p not in ("*", "**", "all", "any")]
        return ", ".join(parts) or None

    transcript = pick(TRANSCRIPT_KEYS)
    if not transcript:
        skip = set(TRIGGER_KEYS) | set(TIME_KEYS) | set(AUDIO_URL_KEYS) | set(IGNORE_KEYS)
        extras = [f"{k}: {v}" for k, v in fields.items() if k not in skip]
        transcript = "\n".join(leftovers + extras)

    return {
        "source": source or pick(SOURCE_KEYS),
        "trigger": clean_trigger(pick(TRIGGER_KEYS)),
        "time": pick(TIME_KEYS),
        "audio_url": pick(AUDIO_URL_KEYS),
        "transcript": (transcript or "").strip(),
    }


# --------------------------------------------------------------------------
# when the transmission happened
# --------------------------------------------------------------------------
# Boondock names each recording after the moment it ENDED, in UTC, and the
# audio URL carries the date too: .../YYYY/MM/DD/HH-MM-SS.mp3. That's free of
# the transcription, email and relay delays the Date header carries — across
# real samples it lands 4-7s before the email is sent, ~30s before it reaches
# AWS. (Boondock doesn't document the zone; every sample agrees with UTC.)

REC_STAMP_RE = re.compile(r"(?:(\d{4})/(\d\d)/(\d\d)/)?(\d\d)-(\d\d)-(\d\d)\.\w+$")


def recording_end(alert, clips, msg):
    """UTC end of recording, or None if neither the URL nor a filename says."""
    path = urllib.parse.urlparse(alert.get("audio_url") or "").path
    hit = REC_STAMP_RE.search(path)
    if hit and hit.group(1):
        try:
            return datetime(*map(int, hit.groups()), tzinfo=timezone.utc)
        except ValueError:      # nonsense digits — fall through
            pass

    # No usable URL: time from the attachment name, date from the email.
    try:
        sent = parsedate_to_datetime(msg["date"]).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
    for name, _ in clips:
        hit = REC_STAMP_RE.search(name)
        if hit:
            try:
                h, mi, sec = map(int, hit.groups()[3:])
                end = sent.replace(hour=h, minute=mi, second=sec, microsecond=0)
            except ValueError:
                continue
            if end > sent:      # recorded before UTC midnight, emailed after
                end -= timedelta(days=1)
            return end
    return None


# --------------------------------------------------------------------------
# message text
# --------------------------------------------------------------------------

def truncate(s, n):
    return s if len(s) <= n else s[: n - 20] + "\n… (truncated)"


def fit(body, suffix, platform):
    """Shorten body so body + suffix fits the platform limit; suffix survives."""
    limit = MAX_TEXT.get(platform, MAX_TEXT["discord"])
    return truncate(body, max(limit - len(suffix), 200)) + suffix


ESCAPES = {
    # Slack mrkdwn: only these three are special, and <!date^…> style tokens
    # must never be escaped — we don't build any, so a blanket escape is safe.
    "slack": [("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")],
    # Discord markdown: backslash-escape the formatting characters instead.
    "discord": [("\\", "\\\\"), ("*", "\\*"), ("_", "\\_"),
                ("~", "\\~"), ("`", "\\`"), ("|", "\\|"), (">", "\\>")],
}


def esc(s, platform="slack"):
    out = str(s)
    for a, b in ESCAPES.get(platform, ESCAPES["slack"]):
        out = out.replace(a, b)
    return out


def alert_time(msg, fallback, platform="slack", recorded=None):
    """
    When the recording ended, rendered with DATETIME_FORMAT in DISPLAY_TZ.
    Falls back to the email's Date header, then to Boondock's own body text.

    Both platforms offer a per-viewer local-time token (Slack's <!date^…>,
    Discord's <t:epoch:t>), but neither can force a 24-hour clock — that
    follows each viewer's own preference. Formatting it here means everyone
    sees the same value.

    The result is NOT escaped: DATETIME_FORMAT is operator config and may
    deliberately contain markup, like the default's _italic_.
    """
    if recorded:
        return recorded.astimezone(_TZ).strftime(DATETIME_FORMAT)

    raw = msg.get("date")
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:  # header lacked an offset
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_TZ).strftime(DATETIME_FORMAT)
        except (TypeError, ValueError):
            pass
    return esc(fallback, platform) if fallback else None


DEFAULT_EMOJI = ":rotating_light:"


def build_message(alert, msg, route=None, matched=None):
    """
    Bold source, matched phrase in inline code, time, transcript as a
    blockquote. Code, _italic_ and blockquote share syntax across Slack mrkdwn
    and Discord markdown; bold and escaping differ per platform.

    Standard emoji shortcodes render on both. Custom emoji do not port: Slack's
    are workspace-scoped and Discord's need <:name:id> syntax.
    """
    route = route or {}
    platform = route.get("platform", "slack")
    source = route.get("label") or alert["source"] or "Radio alert"
    emoji = route.get("emoji") or DEFAULT_EMOJI

    b = BOLD.get(platform, "*")
    parts = [f"{emoji} {b}{esc(source, platform)}{b}"]

    # Prefer what the device reported; fall back to the phrase this route
    # matched on, so transcript-filtered alerts still show what fired them.
    fired_on = alert["trigger"] or matched
    if fired_on:
        parts.append(f"`[{esc(fired_on, platform)}]`")

    when = alert_time(msg, alert["time"], platform, alert.get("recorded"))
    if when:
        parts.append(when)

    lines = [" ".join(parts)]
    if alert["transcript"]:
        body = esc(alert["transcript"], platform)
        lines.append("> " + body.replace("\n", "\n> "))

    # Not truncated here: deliver() may append a fallback note, and the limit
    # has to account for it.
    return source, "\n".join(lines)


def multipart(fields, files):
    """Build a multipart/form-data body. Returns (content_type, bytes)."""
    boundary = "----boondock" + uuid.uuid4().hex
    out = bytearray()

    for name, value in fields.items():
        out += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f"Content-Type: application/json\r\n\r\n{value}\r\n"
        ).encode("utf-8")

    for name, filename, data in files:
        out += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        out += data + b"\r\n"

    out += f"--{boundary}--\r\n".encode("utf-8")
    return f"multipart/form-data; boundary={boundary}", bytes(out)


# --------------------------------------------------------------------------
# HTTP with per-step labels and retry
#
# Every outbound call is a named step ("slack files.completeUploadExternal",
# "slack file upload", "discord create-message"), so a failure in the log says
# which one broke rather than just "HTTP Error 504".
#
# Transient failures (5xx, 429, timeouts, dropped connections) are retried with
# backoff. A 5xx or timeout on a step that makes something VISIBLE — posting a
# message, finalising an upload — is also "ambiguous": the server may have done
# the work and only failed to answer. Retrying those risks a duplicate; not
# retrying risks losing the alert. Losing the alert is worse, so they're
# retried, and the ambiguity is logged and carried through to the fallback.
# --------------------------------------------------------------------------

RETRY_ATTEMPTS = 3
RETRY_BASE = 1.0                      # seconds, doubling: 1s, 2s
TRANSIENT_HTTP = {429, 500, 502, 503, 504}
SLACK_TRANSIENT = {"ratelimited", "internal_error", "fatal_error",
                   "service_unavailable", "request_timeout"}
SLACK_AMBIGUOUS = {"internal_error", "fatal_error", "request_timeout"}

_deadline = None   # monotonic seconds; set per invocation from the Lambda context


def _time_left():
    return float("inf") if _deadline is None else _deadline - time.monotonic()


class StepError(RuntimeError):
    """
    A failure attributed to one named step.

    transient  worth retrying
    ambiguous  the request may have succeeded server-side anyway
    """
    def __init__(self, step, detail, transient=False, ambiguous=False, retry_after=None):
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.transient = transient
        self.ambiguous = ambiguous
        self.retry_after = retry_after


def _parse_retry_after(header, body):
    """Slack sends a Retry-After header; Discord sends retry_after in the body."""
    try:
        if header:
            return float(header)
    except ValueError:
        pass
    try:
        return float(json.loads(body).get("retry_after"))
    except Exception:
        return None


def _http(step, req, timeout, visible=False):
    """urlopen with every failure mode turned into a labelled StepError."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise StepError(
            step, f"HTTP {e.code} {body}".strip(),
            transient=e.code in TRANSIENT_HTTP,
            ambiguous=visible and e.code >= 500,
            retry_after=_parse_retry_after(e.headers.get("Retry-After"), body)
                        if e.code == 429 else None,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise StepError(step, f"network error: {reason}",
                        transient=True, ambiguous=visible) from None


def _retry(fn):
    """
    Run fn(), retrying transient StepErrors with exponential backoff. Never
    starts a retry the invocation can't finish. If ANY attempt was ambiguous,
    the final outcome carries that — a success is logged as a possible
    duplicate, and a failure is marked ambiguous for the fallback.
    """
    ambiguous = False
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            result = fn()
            if ambiguous:
                print("  ...succeeded after an ambiguous failure — "
                      "a duplicate post is possible")
            return result
        except StepError as e:
            ambiguous = ambiguous or e.ambiguous
            e.ambiguous = ambiguous
            if not e.transient or attempt == RETRY_ATTEMPTS:
                raise
            wait = min(e.retry_after or RETRY_BASE * 2 ** (attempt - 1), 10)
            if _time_left() < wait + 20:
                print(f"{e} — not retrying, only {_time_left():.0f}s left")
                raise
            print(f"{e} — retrying in {wait:.1f}s (attempt {attempt}/{RETRY_ATTEMPTS})")
            time.sleep(wait)


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------

def _slack_call(token, method, body, ctype, visible=False, timeout=15):
    step = f"slack {method}"
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=body, method="POST",
        headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"})
    _, raw = _http(step, req, timeout, visible)

    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise StepError(step, f"non-JSON response {raw[:200]!r}",
                        transient=True, ambiguous=visible) from None

    if not data.get("ok"):
        err = data.get("error")
        extra = " ".join(f"{k}={data[k]}" for k in ("needed", "provided", "response_metadata")
                         if data.get(k))
        raise StepError(step, f"{err} {extra}".strip(),
                        transient=err in SLACK_TRANSIENT,
                        ambiguous=visible and err in SLACK_AMBIGUOUS)
    return data


def slack_json(token, method, payload, visible=False):
    return _slack_call(token, method, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8", visible)


def slack_form(token, method, params):
    return _slack_call(token, method, urllib.parse.urlencode(params).encode("utf-8"),
                       "application/x-www-form-urlencoded")


def put_file(upload_url, filename, data):
    ctype, body = multipart({}, [("file", filename, data)])
    req = urllib.request.Request(
        upload_url, data=body, headers={"Content-Type": ctype}, method="POST")
    return _http("slack file upload", req, timeout=20)[0]


def _stage_clip(token, filename, data):
    """
    Reserve a slot and send the bytes. Retried as a PAIR: a failed transfer
    gets a fresh slot rather than re-sending to a URL whose state is unknown.
    An abandoned slot is never finalised, so it never becomes visible.
    """
    slot = slack_form(token, "files.getUploadURLExternal", {
        "filename": filename, "length": len(data),
    })
    put_file(slot["upload_url"], filename, data)
    return slot["file_id"]


def slack_post(token, channel, text, clips, title):
    """
    Slack's three-step external upload. The upload IS the post — a file can't
    be attached to an existing chat.postMessage — so the text rides along as
    initial_comment.
    """
    if not clips:
        _retry(lambda: slack_json(token, "chat.postMessage", {
            "channel": channel, "text": text, "unfurl_links": False,
        }, visible=True))
        return

    files = []
    for filename, data in clips:
        file_id = _retry(lambda: _stage_clip(token, filename, data))
        files.append({"id": file_id, "title": f"{title} — {filename}"})
        print(f"staged {filename} ({len(data)} bytes)")

    _retry(lambda: slack_json(token, "files.completeUploadExternal", {
        "files": files, "channel_id": channel, "initial_comment": text,
    }, visible=True))


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "boondock-relay (https://github.com/, 1.0)"


def discord_post(token, channel, text, clips, title):
    """
    One request, unlike Slack: attachments ride in the same multipart body as
    the message. Discord requires a User-Agent on bot requests, and the auth
    scheme is the literal word "Bot", not "Bearer".
    """
    url = f"{DISCORD_API}/channels/{channel}/messages"
    payload = json.dumps({
        "content": text,
        "allowed_mentions": {"parse": []},   # never ping @everyone from a transcript
    })
    headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}

    if clips:
        files = [(f"files[{i}]", name, data) for i, (name, data) in enumerate(clips)]
        ctype, body = multipart({"payload_json": payload}, files)
    else:
        ctype, body = "application/json", payload.encode("utf-8")
    headers["Content-Type"] = ctype

    def once():
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        return _http("discord create-message", req, timeout=30, visible=True)[0]

    return _retry(once)


SENDERS = {"slack": slack_post, "discord": discord_post}


def deliver(route, alert, msg, clips, matched=None):
    """Post one alert to one destination. Raises if the alert can't be posted."""
    platform = route.get("platform", "slack")
    send = SENDERS[platform]
    token, channel = route["token"], route["channel"]
    header, text = build_message(alert, msg, route, matched)

    if clips:
        try:
            send(token, channel, fit(text, "", platform), clips, header)
            return True
        except Exception as exc:
            # Losing the audio is bad; losing the alert is worse. Fall through.
            print(f"audio post failed for {platform} {channel}, falling back to text: {exc}")
            if getattr(exc, "ambiguous", False):
                note = ("the upload timed out on the server side — the version "
                        "with audio may still appear")
            else:
                note = f"audio upload failed at {getattr(exc, 'step', 'upload')}"
            suffix = f"\n_{esc(note, platform)}_"
            if alert["audio_url"]:
                suffix += f"\n{alert['audio_url']} (link expires)"
            send(token, channel, fit(text, suffix, platform), [], header)
            return False

    send(token, channel, fit(text, "", platform), [], header)
    return False


def lambda_handler(event, context):
    global _deadline
    try:   # leave a few seconds' margin to log and return cleanly
        _deadline = time.monotonic() + context.get_remaining_time_in_millis() / 1000 - 3
    except Exception:
        _deadline = None   # hand-run tests with no context

    ses = event["Records"][0]["ses"]
    mail = ses["mail"]
    message_id = mail["messageId"]

    sender = (mail.get("source") or "").lower()
    if ALLOWED and not any(sender == a or sender.endswith("@" + a) for a in ALLOWED):
        print(f"dropping message from {sender}")
        return {"status": "ignored", "reason": "sender"}

    reason, verdicts = check_receipt(ses.get("receipt", {}))
    print(f"verdicts: {verdicts}")
    if reason:
        print(f"dropping message from {sender}: {reason}")
        return {"status": "ignored", "reason": reason, "verdicts": verdicts}

    obj = s3.get_object(Bucket=BUCKET, Key=f"{PREFIX}{message_id}")
    msg = BytesParser(policy=policy.default).parsebytes(obj["Body"].read())

    alert = parse_alert(extract_text(msg))
    clips = extract_audio(msg)
    alert["recorded"] = recording_end(alert, clips, msg)

    recipients = ses.get("receipt", {}).get("recipients") or mail.get("destination") or []
    targets = collect_routes(load_routes(), recipients, alert)

    print(f"to={recipients} source={alert['source']!r} trigger={alert['trigger']!r} "
          f"clips={[c[0] for c in clips]} targets={[t[0] for t in targets]} "
          f"recorded={alert['recorded'].isoformat() if alert['recorded'] else 'unknown'}")

    if not targets:
        print(f"no route for {recipients} / {alert['source']!r} and no default — dropping")
        return {"status": "ignored", "reason": "no route"}

    # One failing destination must not stop the others, so each is independent.
    results = []
    for why, route, matched in targets:
        try:
            with_audio = deliver(route, alert, msg, clips, matched)
            print(f"posted to {route['channel']} via {why} (audio={with_audio})")
            results.append({"route": why, "channel": route["channel"], "ok": True,
                            "audio": with_audio})
        except Exception as exc:
            print(f"FAILED posting to {route.get('channel')} via {why}: {exc}")
            results.append({"route": why, "channel": route.get("channel"), "ok": False,
                            "error": str(exc)})

    delivered = sum(1 for r in results if r["ok"])
    return {"status": "ok" if delivered else "failed",
            "delivered": delivered, "targets": len(results), "results": results}
