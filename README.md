# media-flow-youtube

![CI](https://github.com/OwenTanzer/media-flow-youtube/actions/workflows/ci.yml/badge.svg)

A lightweight backend that fetches YouTube transcripts on demand or on a
schedule, and archives them to a Google Drive folder as clean Markdown
files (with metadata frontmatter) plus a JSON index. Built to run as a
single small service on Railway.

## How it works

- `POST /transcripts` — give it one or more YouTube URLs (or bare video
  IDs), it fetches each caption track via `youtube-transcript-api`, looks
  up the title/channel via YouTube's public oEmbed endpoint, and writes a
  Markdown file per video to your Drive folder.
- `POST /batch/run` — either processes an explicit list of URLs, or (if
  none given) reads `queue.json` from the same Drive folder, processes
  everything in it, and rewrites the queue with only the entries that
  failed transiently. Add new videos to the queue any time by editing
  `queue.json` directly in Drive.
- Every fetch (success or failure) is recorded in `_index.json` in the
  folder, keyed by video ID, so you always know what's been tried and
  where to find it.
- `discover_and_process.py` — optionally, poll a set of YouTube channels
  (configured in `channels.json`, also in Drive) for new uploads via the
  YouTube Data API (or public RSS feeds as a fallback), queue the ones not
  seen before, process the queue, and then turn each successful transcript
  into a structured, timestamped Claude-generated summary
  (`summaries/<video_id>.json`), all in one scheduled run. See "Scheduled
  channel discovery" and "Transcript summarization" below.
- Missing/disabled captions, unavailable videos, and IP blocks are all
  caught and reported as a `status` field — the service never crashes on
  a single bad video.

Because everything lives in one Drive folder as plain Markdown + JSON,
Claude (or you) can browse, search, and read the transcript archive
directly from Drive at any time — no database, no extra API to query.

## Transcript file format

Each video becomes `{title} [{video_id}].md`:

```markdown
---
video_id: dQw4w9WgXcQ
title: "Never Gonna Give You Up"
url: https://www.youtube.com/watch?v=dQw4w9WgXcQ
channel: Rick Astley
fetched_at: 2026-07-13T10:41:55+00:00
published_at: 2026-07-10T14:00:00+00:00
language: English (en)
auto_generated: false
---

[00:00] We're no strangers to love
[00:05] You know the rules and so do I
...
```

`published_at` - the video's real YouTube publish time - is only present for
videos discovered via `discover_and_process.py`'s RSS feed reader, the only
source that sees it; a manually-added `queue.json` entry or a direct
`/transcripts` URL has no such source and omits the field entirely rather
than guessing (`fetched_at` can lag the real upload by anywhere from
minutes to a full discovery interval, so it's not a substitute). It's
carried through to `_index.json` and, for summarized videos, to the
summary artifact's `video_published_at` - see "Transcript summarization"
below - since sorting market/news content by when it was actually said
(not by processing order) matters for a future consumer like a visualizer.

## Setup

### 1. Get OAuth credentials for your Google account

The app authenticates to Drive as **your own Google account**, not a
service account — service accounts have zero Drive storage quota and
can't create new files in a personal ("My Drive") folder, only edit
files someone else already owns. Since a personal Gmail account can't
use Shared Drives or domain-delegated service accounts (those require
Google Workspace), OAuth user credentials are the fix.

1. In the [Google Cloud Console](https://console.cloud.google.com/), create
   (or reuse) a project and enable the **Google Drive API**.
2. Under **APIs & Services → Credentials**, create an **OAuth client ID**
   of type **Desktop app**. Note the client ID and client secret.
3. Locally, run the one-time authorization script to mint a refresh token:
   ```bash
   pip install google-auth-oauthlib
   python get_refresh_token.py
   ```
   This opens a browser for you to sign in and grant Drive access, then
   prints `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
   `GOOGLE_OAUTH_REFRESH_TOKEN` — set all three wherever the app runs.
4. In Google Drive, create the folder you want transcripts archived to.
   No sharing step is needed — you own it, since the app now acts as you.
5. Copy the folder's ID from its URL (`https://drive.google.com/drive/folders/<THIS_PART>`).

Treat the client secret and refresh token like passwords: set them only
in Railway's environment variables (or a local, gitignored `.env`) —
never commit them, and never paste them into a public place.

**Before deploying long-term**, check your OAuth consent screen's
publishing status under **APIs & Services → OAuth consent screen**. Left
in **Testing**, Google expires refresh tokens after 7 days — fine for a
local smoke test, but it'll silently break the deployed service a week
in. Move it to **Production** (no Google verification review is required
for personal, single-user use like this) so the refresh token doesn't
expire. If a token does expire, just rerun `get_refresh_token.py` and
update the env var. See
[Google's docs on refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

### 2. Configure environment variables

See [`.env.example`](.env.example) for the full list. At minimum:

| Variable | Description |
|---|---|
| `DRIVE_FOLDER_ID` | The Drive folder ID from step 1. |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` / `GOOGLE_OAUTH_REFRESH_TOKEN` | From `get_refresh_token.py` above. |
| `API_KEY` | Shared secret required in the `X-API-Key` header. The app refuses to start without it unless `DRY_RUN=true`, since the deployed URL is otherwise public and unauthenticated. |

### 3. Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in values, or export them directly
export $(grep -v '^#' .env | xargs)   # or use a tool like direnv/foreman
uvicorn app.main:app --reload
```

Set `DRY_RUN=true` to test the API and transcript pipeline without a real
Drive folder or credentials — Drive writes are logged instead of sent.

### Testing

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

The test suite mocks YouTube/Drive/oEmbed calls throughout, so it runs
offline with no real credentials. It also covers the failure-isolation
behavior called out below (a bad video can't 500 a whole request, and a
failed index update can't erase a successful archive). GitHub Actions
(`.github/workflows/ci.yml`) runs both on every push and pull request.

### 4. Deploy to Railway

1. Push this repo to GitHub and create a new Railway project from it (or
   `railway up` from the CLi).
2. Set the environment variables from step 2 in the Railway service
   settings.
3. Railway will detect `railway.toml` / `Procfile` and run
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT` automatically.
4. Once deployed, Railway gives you a public URL — that's your endpoint,
   e.g. `https://your-app.up.railway.app/transcripts`.

### 5. Working around IP blocks (optional, but likely needed)

`youtube-transcript-api` fetches captions directly from YouTube, which
blocks known cloud-provider IP ranges (Railway's included) far more
aggressively than residential IPs. If you see `"status": "blocked"` in
responses, route requests through a proxy via `YOUTUBE_PROXY_TYPE` (see
[`.env.example`](.env.example) for every variable) - no code or API
changes needed, just configuration:

| `YOUTUBE_PROXY_TYPE` | Use when | Required variables |
|---|---|---|
| unset (default) | Requests direct, no proxy | — |
| `webshare` | Using [Webshare](https://www.webshare.io/), which the library has built-in retry/rotation support for. It has a permanent free tier (10 datacenter IPs, 1GB/month) worth trying first - though datacenter IPs may still get blocked the same way Railway's do. Their paid "Residential" tier is the reliable fix. | `WEBSHARE_PROXY_USERNAME`, `WEBSHARE_PROXY_PASSWORD` (from the [Webshare dashboard](https://dashboard.webshare.io/proxy/settings)) |
| `generic` | Any other HTTP/HTTPS proxy, including a self-hosted tunnel back to your own residential IP (e.g. via Cloudflare Tunnel or Tailscale) | `YOUTUBE_PROXY_HTTP_URL` and/or `YOUTUBE_PROXY_HTTPS_URL`, e.g. `http://user:pass@host:port` |

The app validates this configuration at startup — an invalid or
incomplete proxy setup fails the deployment immediately rather than
surfacing later as per-video `blocked`/`error` results. A working but
rejected proxy still produces the existing safe behavior: affected
videos come back as `blocked` and stay queued for retry, exactly as
without a proxy.

**Rotating/updating credentials:** proxy credentials are read fresh from
environment variables on every YouTube request rather than cached, so
rotating them is just updating the Railway variables and letting the
service restart (Railway does this automatically on a variable change).
No redeploy of code is required. As with all secrets in this project,
set proxy credentials only in Railway's environment variables (or a
local, gitignored `.env`) - never commit them.

**Note on `WEBSHARE_PROXY_USERNAME`:** use the *bare* username from your
Webshare dashboard, not a sticky-session variant (e.g. one with a
`-<country>-<n>` suffix already appended). `youtube-transcript-api`
appends its own `-rotate` suffix to whatever username you provide, so a
username that already has a session suffix baked in produces an invalid
combined string and proxy auth fails. A bare username lets it correctly
draw a fresh IP from the rotating pool on every request - verified by
hitting an IP-echo endpoint through the proxy repeatedly and confirming
the IP actually changes each time, rather than staying fixed on one
sticky IP.

**A single rotating-proxy draw has a fixed chance of landing on an
already-flagged exit IP - and this doesn't improve by waiting.** An
earlier investigation into a large live run's rising failure rate
initially assumed the pool was being "worn out" by sustained request
volume and needed a cooldown to recover. Controlled follow-up testing
disproved that: a fresh, completely unpaced single request against a
video that had just failed in production succeeded at roughly the same
~80-85% rate whether tested immediately, mid-run, or after a 5-minute
gap - and independent draws against Webshare's own IP-echo endpoint
showed the rotating pool handing out distinct, healthy IPs throughout,
even while the "degraded" run was still failing. So the actual
per-request failure rate looks like a fairly constant property of the
pool at any given moment (some fraction of exit IPs are already flagged
by YouTube), not something that accumulates with our own volume or is
fixed by resting.

What actually fixes it: `fetch_transcript()` (see `app/youtube.py`)
retries a blocked or network-flaky attempt up to
`TRANSCRIPT_FETCH_MAX_ATTEMPTS` times (default `3`), each attempt using a
brand-new `YouTubeTranscriptApi` instance - a fresh connection, and thus
another independent draw from the rotating pool - rather than relying on
the library's own internal same-session retry (which testing showed
doesn't reliably escape a blocked IP) or giving up after a single bad
draw. At a ~80% single-shot success rate, 2-3 independent draws pushes
effective success into the high 90s%; this was confirmed directly by
retrying videos that had actually failed in a live production run, most
of which succeeded on the very next fresh attempt.

`run_batch()` (see `app/batch.py`) still processes queued entries in
chunks of `BATCH_SIZE_THRESHOLD` (default `10`), checkpointing
`queue.json` after every chunk so a crash partway through a long run
doesn't lose already-completed progress - that's now chunking's only
job. `BATCH_COOLDOWN_SECONDS` (default `0`) no longer defaults to a real
pause between chunks, since checkpointing doesn't require sleeping and
the pool-recovery rationale for the old 300s default didn't hold up;
only raise it if independent evidence shows a nonzero delay helps,
since a large backlog with both a nonzero cooldown and per-video retries
can idle for a long time otherwise. Both settings must be
positive/non-negative - the app refuses to start with an invalid value,
since a threshold `<= 0` would otherwise chunk the queue into zero
batches and silently overwrite `queue.json` with an empty list.
Chunking only applies to the
queue-driven path (`discover_and_process.py`/`batch_runner.py`) - an
explicit URL list (e.g. a live `POST /batch/run` request) is never
paced, since a large one would otherwise hold the HTTP connection open
for the full cooldown duration. Because a large queue's total cooldown
time can exceed `DISCOVERY_LOCK_TTL_SECONDS`, `discover_and_process.py`
renews its lock lease after every chunk (`job_lock.renew_lock()`) so a
healthy long run doesn't look crashed to a concurrent invocation; if the
lease is ever found to belong to someone else mid-run, the run aborts
immediately rather than continuing to write against a lock it no longer
holds.

## Usage

```bash
curl -X POST https://your-app.up.railway.app/transcripts \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]}'
```

```bash
curl -X POST https://your-app.up.railway.app/batch/run \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Response shape (per video):

```json
{
  "video_id": "dQw4w9WgXcQ",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "status": "ok",
  "title": "Never Gonna Give You Up",
  "filename": "Never Gonna Give You Up [dQw4w9WgXcQ].md",
  "drive_file_id": "1AbC...",
  "message": null
}
```

`status` is one of: `ok`, `no_captions`, `unavailable`, `blocked`,
`invalid_url`, `error`.

## Scheduled batch processing (optional)

Two ways to run `POST /batch/run` on a schedule — pick one:

**A. In-process scheduler (simplest, one Railway service)**

Set `ENABLE_SCHEDULER=true` and `SCHEDULE_CRON` (a standard 5-field
crontab expression, UTC) on the web service. It runs the queue batch job
on that schedule inside the same process, no extra deployment needed.

**B. Separate Railway Cron Job service**

Deploy `batch_runner.py` as its own Railway service using the
[Cron Job](https://docs.railway.com/guides/cron-jobs) deployment type,
with start command `python batch_runner.py`, pointed at the same
environment variables. Railway runs it on your schedule and shuts it down
between runs — no idle web process needed.

## Scheduled channel discovery (optional)

Instead of (or as well as) manually editing `queue.json`, you can point
the app at a set of YouTube channels and have it discover new uploads on
its own, via either the official YouTube Data API or each channel's public
RSS feed.

### Discovery source: YouTube Data API vs RSS (issue #24)

Public RSS feeds are the default and require no extra credentials, but
they've proven unreliable in production for several channels (404/500
responses even though the channel is fine). Setting `YOUTUBE_DATA_API_KEY`
switches `discover_and_process.py` to the official YouTube Data API's
uploads-playlist flow instead, **exclusively** - RSS is only ever used as
the fallback while that key is unset, never alongside the API for the same
channel.

How it works: for each configured channel, `app/youtube_data_api.py`
resolves `contentDetails.relatedPlaylists.uploads` via `channels.list`
(cached forever afterward in `_uploads_playlists.json` in the Drive
folder, so this only costs one quota unit per channel, ever), then polls
that playlist via `playlistItems.list` - which is ordered newest-first, so
polling stops as soon as an already-known video ID is seen. Steady-state
hourly polling therefore costs exactly one `playlistItems.list` unit per
channel per run; a brand-new channel with no known videos yet pages
through its full upload history. With ~12 channels polled hourly this is
far under the default 10,000-unit daily quota.

To enable it: enable YouTube Data API v3 for a Google Cloud project
([API library](https://console.cloud.google.com/apis/library/youtube.googleapis.com)),
create an API key ([credentials](https://console.cloud.google.com/apis/credentials) -
a separate, simpler credential from the `GOOGLE_OAUTH_*` ones used for
Drive), and set `YOUTUBE_DATA_API_KEY` on the production
`discover-and-process` Railway service.

### 1. Create `channels.json` in the Drive folder

```json
{
  "version": 1,
  "channels": [
    {
      "channel_id": "UC_x5XG1OV2P6uZZ5FSM9Ttw",
      "name": "Google for Developers",
      "enabled": true,
      "group": "Google"
    },
    {
      "channel_id": "UCsomeSpanishChannelId",
      "name": "Some Spanish-language channel",
      "enabled": true,
      "languages": ["es", "en"]
    }
  ]
}
```

- `channel_id` is the stable `UC…` channel ID (not the `@handle`) — find
  it in a channel's page source, or via any "channel ID lookup" tool.
- `enabled: false` skips a channel without deleting its entry.
- `languages` is optional; when set, it overrides `TRANSCRIPT_LANGUAGES`
  for videos discovered from that channel only.
- `group` is optional and drives the top-level tabs in the Streamlit
  dashboard (see "Insight dashboard" below) - it defaults to `"Finance"`
  when absent, so only channels that should appear under a different
  group (currently just `"Google"`) need to set it explicitly.

No deployment is needed to add, remove, enable, or disable a channel —
just edit `channels.json` in Drive, same as `queue.json`. Alternatively,
if `VIDPROC_ADMIN_TOKEN` is set (see "Insight dashboard" below), the
dashboard's token-gated **Admin** tab can add a channel through a form
and immediately backfill its current RSS feed, without touching Drive
directly.

### 2. Deploy `discover_and_process.py` as a Railway Cron Job

One job does both steps every run: discover new uploads from enabled
channels, queue the ones not already in `_index.json` or `queue.json`,
then process the queue exactly like `batch_runner.py` does. Deploy it as
its own [Cron Job](https://docs.railway.com/guides/cron-jobs) service
with start command `python discover_and_process.py`, pointed at the same
environment variables (plus `channels.json` in the same Drive folder).

**Concurrency invariant:** `queue.json` and `_index.json` use unlocked
read-modify-write Drive operations that only tolerate one writer at a
time. Discovery and queue processing must therefore run as one
serialized job, never as independent, potentially-overlapping ones — do
not *also* enable `ENABLE_SCHEDULER` or deploy `batch_runner.py` on a
schedule alongside `discover_and_process.py`; pick one queue-processing
path. `discover_and_process.py` additionally takes out an advisory
Drive-based lock (`_discovery_lock.json` in the same folder) for its own
duration, so a second overlapping invocation of *itself* (e.g. a manual
run while a scheduled one is still going) exits immediately instead of
racing it. `DISCOVERY_LOCK_TTL_SECONDS` (default `1800`) controls how old
that lock must be before a new run assumes the previous one crashed and
proceeds anyway.

This lock is deliberately advisory, not a true distributed
compare-and-swap (that redesign - Drive revision/ETag preconditions plus
retry, or a transactional shared store - is explicitly out of scope for
this feature). Its acquire is a check-then-write that isn't atomic across
processes, and Drive additionally permits duplicate filenames in one
folder, so two near-simultaneous invocations could otherwise both believe
they hold the lock. Two things narrow that window: every lock carries a
random ownership token, so a run can only ever delete or take over a
lease it actually recognizes (a slow/crashed run can't steal or clear a
*different* run's active lock); and immediately after writing its lock,
a run re-checks that exactly one lock file exists and it's the one that
run just wrote, backing off if a concurrent writer is detected. Real
overlap (e.g. Railway Cron drift plus a manual run) is caught; a
sub-second true race at the Drive API level is not fully eliminated.

**Recovery:** if a run crashed and you're certain nothing is actually in
flight, you don't have to wait out the TTL — just delete
`_discovery_lock.json` from the Drive folder and the next run will
acquire the lock immediately.

### Backfilling a newly added channel

A channel just added to `channels.json` gets its currently-visible RSS
feed queued the same way as any other channel the next time
`discover_and_process.py` runs (up to `DISCOVERY_LOCK_TTL_SECONDS`/the
cron schedule away) — no separate step is strictly required. If you'd
rather not wait, run `python backfill_new_channels.py` once: it finds
every enabled channel with zero videos anywhere in `_index.json` or
`queue.json` yet (i.e. never discovered at all) and queues whatever's
currently in that channel's feed. Idempotent — a channel already
backfilled is skipped on rerun.

It shares `discover_and_process.py`'s own `_discovery_lock.json` rather
than a lock of its own — both write the same `queue.json`, and a
distinct lock would only serialize this script against itself while
doing nothing to stop it from interleaving with (and silently corrupting)
a concurrently-running `discover_and_process.py`. It does **not** wait
for that lock, though: if `discover_and_process.py` is already running,
this exits immediately rather than blocking — that run (or the next one)
will pick up the new channel regardless, so running this never
introduces a waiting period of its own.

### Livestreams

YouTube's RSS feed lists a livestream as soon as it starts, well before
it ends or captions exist for it. Without special handling, discovery
would try to fetch its transcript immediately, get "no_captions", and -
since that's normally a terminal status - drop the video for good,
losing it even after the stream ends and captions become available.

To avoid that, every video discovery queues carries a `first_seen_at`
timestamp, and a "no_captions" result for such a video is retried on
every subsequent run rather than treated as final, until it's older than
`NO_CAPTIONS_GRACE_HOURS` (default `24`) - long enough to cover even a
multi-hour livestream plus YouTube's post-stream caption-processing
delay. Past that window, it's treated as genuinely caption-less and
dropped, same as any other video. Videos added manually to `queue.json`
(no `first_seen_at`) get no grace period - "no_captions" is terminal for
them immediately, exactly as before this existed.

## Transcript summarization (optional)

> Two summary builders exist - see [Which summary builder to use](#which-summary-builder-to-use).

> **Current execution model:** `summarize_backlog.py` is a separate Railway
> Cron Job (`railway.summarize-backlog.toml`), not a stage of
> `discover_and_process.py`. It reads successful transcripts and writes only
> independent `summaries/<video_id>.json` artifacts, so it does not acquire
> the discovery/queue Drive lock. It runs up to four summaries concurrently;
> each eligible video gets one structured attempt. Failed artifacts are
> eligible again on the next worker run.
> There is no token-count preflight, per-run budget gate, retry timestamp, or
> permanent attempt-count lockout.

`summarize_backlog.py` turns every successfully archived transcript
(`status: "ok"` in `_index.json`) into a structured,
timestamped JSON insight artifact via Claude - `summaries/<video_id>.json`
in the same Drive folder. It never touches `no_captions`/`blocked`/etc.
entries, and a failure summarizing one video never blocks transcript
discovery or archiving, since it runs strictly after both of those
complete.

**Replacement policy (issue #26):** a video with a completed (`status:
"ok"`) summary is skipped on every subsequent run, full stop - not just
while its `source_transcript_hash`/`model`/`prompt_version`/taxonomy still
match what produced it. A published summary is authoritative by default;
the normal backlog job never silently regenerates or overwrites one, even
after a transcript re-fetch, a `SUMMARY_MODEL` change, a `PROMPT_VERSION`
bump, or an edited group's `video_types`. Only a `status: "error"` artifact
(or no artifact at all) stays eligible every run. The only way to replace a
completed summary is the explicit, targeted opt-in:
`SUMMARY_FORCE_RESUMMARIZE_VIDEO_IDS` (see [`.env.example`](.env.example)) -
a comma-separated video ID list set for one run, then unset again. Each run
logs how many videos were skipped for already having a completed summary
versus force-resummarized versus genuinely new/failed work, so queue status
stays legible (`app/backlog_summarizer.py`'s `SummaryReport.skipped_current`
vs. `.forced` vs. `.eligible`/`.failed`).

### Setup

Set `ANTHROPIC_API_KEY` wherever the independent summary worker runs. This
stage is optional: if the key is absent, the worker exits cleanly and
discovery/transcript archiving continue unaffected. Configure the Railway
Cron Job with `python summarize_backlog.py` (the included
`railway.summarize-backlog.toml` runs it every five minutes). See
[`.env.example`](.env.example) for the Haiku model, response-size, transcript
length, and worker-concurrency settings.

### Output format

```json
{
  "video_id": "dQw4w9WgXcQ",
  "source_drive_file_id": "1AbC...",
  "source_transcript_hash": "sha256:...",
  "title": "Never Gonna Give You Up",
  "author": "Rick Astley",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "video_published_at": "2026-07-10T14:00:00+00:00",
  "channel_id": "UC_x5XG1OV2P6uZZ5FSM9Ttw",
  "video_type": "Analytic Overview",
  "summary": "...",
  "points": [
    {"importance": "major", "main_point": "...", "explanation": "...", "timestamp_seconds": 12, "timestamp": "00:12"}
  ],
  "status": "ok",
  "model": "claude-haiku-4-5",
  "prompt_version": "v6",
  "generated_at": "2026-07-14T12:00:00+00:00",
  "attempts": 1,
  "usage": {"input_tokens": 1234, "output_tokens": 456, "estimated_cost_usd": 0.0035}
}
```

`title`, `author`, `url`, `video_published_at`, `channel_id`, the source
Drive file ID, and the transcript hash are always populated by application
code from `_index.json` and the archived transcript file itself - never
trusted from the model's output. `video_published_at` and `channel_id` are
both `null` unless the video was discovered via RSS (see "Transcript file
format" above) - there's no other source for either. `channel_id` is the
stable `channels.json` ID, not the free-text `author` name embedded in the
transcript - a downstream consumer that needs to reliably match a summary
back to its channel registry entry (e.g. the Streamlit dashboard, issue #8)
should join on `channel_id`, not `author`.
This is the only artifact shape written by the current summary worker.
Claude only produces `video_type`, `summary`, and each point's
`importance`, `main_point`, `explanation`, `source_timestamp`, and
`source_anchor`, constrained by a JSON schema (`output_config.format`) so
the response is always valid JSON with no Markdown fences or surrounding
prose. `video_type`'s allowed categories are **per-group**, not fixed -
see "Per-group video types (groups.json)" below - but still enforced by
the schema itself for whichever group applies (a `Literal` built
dynamically per call, not a free-text field), so an unrecognized
classification is rejected the same way a missing field would be. Every
string field and the `points` list itself also require at least one
character/item, since an empty response is otherwise schema-valid
despite not being a usable summary.

### Per-group video types (groups.json)

Each dashboard group (see "group" in `channels.json` above) has its own
`video_type` classification taxonomy - the finance-flavored default
categories (`"Post-Market Update"`, `"Pre-Market Brief"`, `"Thesis
Piece"`, `"Analytic Overview"`) don't make sense for, say, a "Google"
group's product/tutorial content. Configure this in `groups.json` in the
same Drive folder:

```json
{
  "version": 1,
  "groups": [
    {"name": "Finance", "video_types": ["Post-Market Update", "Pre-Market Brief", "Thesis Piece", "Analytic Overview"]},
    {"name": "Google", "video_types": ["Short Showcase", "Tutorial"]}
  ]
}
```

A group with no entry here (or an empty `video_types` list) falls back
to `DEFAULT_GROUP`'s (Finance's) configured list, and finally to the
hardcoded default categories above if neither has anything configured at
all - so a deployment with no `groups.json` set up behaves exactly as it
did before this feature existed. The Admin tab's "+ Create a new group"
flow (see "Admin panel" below) requires specifying at least one video
type up front, so a new group is never left unconfigured by accident.

A description can optionally be attached to each category too
(`"video_type_descriptions": {"Post-Market Update": "a recap/review of a
trading session..."}`) - the model classifies meaningfully more reliably
against a real definition than a bare name alone (this is how Finance's
own default categories are defined - see
`summarize.FALLBACK_VIDEO_TYPE_DESCRIPTIONS`). Not editable from the
admin panel's create/edit-group forms (those only take names) - add it by
hand-editing `groups.json` directly, same as e.g. `channels.json`'s
`languages` field.

Editing a group's `video_types`/`video_type_descriptions` **is**
retroactive, just not immediately: each summary artifact records a
`video_types_fingerprint` (a hash of the exact taxonomy it was classified
against), and `needs_summarization()` treats a changed fingerprint as a
new unit of work, exactly like a changed transcript hash/model/prompt
version - so any video previously summarized under a different taxonomy
becomes eligible for re-summarization again on the *next*
`discover_and_process.py` run, at that run's usual API cost. This also
means seeding `groups.json` late (after the scheduled job has already run
at least once under the fallback categories) self-corrects on the next
run rather than leaving those videos permanently misclassified.

An existing group's `video_types` can be edited or deleted later too, via
the Admin tab's "Manage groups" section (an expander per group, with
"Save" to replace its list and "Delete" to remove its configuration
entirely) - see "Admin panel" below. Neither operation renames a group or
touches `channels.json`: deleting a group's configuration just makes any
channel still assigned to it fall back to the default group's
`video_types` (or the hardcoded default) until it's reconfigured or the
channel is moved to a different group.

**`timestamp_seconds` is never trusted from the model at all - not even as
a self-reported number.** An earlier version asked the model to compute
`timestamp_seconds` directly and only range-checked the result, which let
a wrong-but-plausible value through whenever the model mis-remembered
*where* in the transcript something was said (worst on videos that
revisit the same topic more than once - the model would sometimes report
a timestamp synthesized or averaged across several mentions instead of
one real line). Instead, the model is asked only to copy visible evidence:
`source_timestamp` must be one transcript line's own bracketed timestamp,
verbatim, and `source_anchor` a short verbatim excerpt from that same
line. Application code then verifies both - `source_timestamp` must parse
and exactly match one of the transcript's own real line timestamps (not
merely fall within range), and `source_anchor` must actually appear
(case/whitespace-insensitive) within two lines of that timestamp - before
computing the final `timestamp_seconds` and the human-readable `timestamp`
string (via the same formatter transcript Markdown files use) itself.
Neither field the model produces ever reaches the persisted artifact
directly; both are inputs to a verification step that either produces a
trustworthy `timestamp_seconds` or fails the point. Points are **not**
required to be in chronological order: real videos (livestreams
especially) revisit the same topic more than once, and an earlier
strict-ordering requirement rejected genuinely well-formed output for that
content in testing - the `explanation` may still synthesize across every
mention of a revisited topic, it's only the citation itself that must
point at one real line. A failed attempt (provider error, safety refusal,
unparseable/invalid structured output, an unrecognized `source_timestamp`,
or a `source_anchor` that doesn't check out) writes `status: "error"` with
a `message`, a `retryable` flag, and the `attempts` count so far instead -
this includes the case where the SDK's own response-parsing step raises a
schema validation error directly (a `pydantic.ValidationError`, not one of
the SDK's own exception types), which would otherwise escape per-video
isolation and abort the whole run.

### Point count

The number of points is otherwise unbounded, which would let a long,
information-dense video (a multi-hour livestream, say) produce an
unreasonably large list. A per-video cap - roughly one point per 3 minutes
of video, up to a ceiling of 20 regardless of length - is built into the
system prompt sent to the model, and enforced again as a hard backstop
after the response comes back: if the model still returns more points
than the cap allows, only the most significant ones are kept (`"major"`
points before `"minor"` ones, preserving the model's original relative
order among whichever are kept), and the artifact is flagged
`"points_truncated": true`. This is a selection, not a validation failure
- it never triggers a retry, since keeping fewer of the model's own points
isn't a correctness problem with any individual one. Short videos aren't
affected in practice: the prompt's own guidance already keeps them to a
handful of points well under the cap.

Routine housekeeping/administrative content - schedule or
streaming-cadence announcements, membership/Patreon/sponsor plugs,
"welcome back"/sign-off preambles, like-and-subscribe asks - is excluded
from points by default, even when the transcript spends real time on it.
This matters more the longer the video is: with a fixed point cap, every
point spent on channel admin is one less available for actual substantive
content. The exception is housekeeping that's itself substantively
important (e.g. a schedule change that affects when to expect the next
analysis), which can still get a point. Each point's `explanation` is
guided to be 2-4 sentences rather than 1-3, favoring real specifics
(numbers, reasoning, context) over terseness.

### Idempotency and retries

> **This section describes `app/summary_store.py`'s `summarize_eligible()`,
> which is not currently called anywhere (superseded by the simpler
> `app/backlog_summarizer.py` described above) - kept for reference in case
> that richer retry/fallback machinery is reinstated. For the worker
> actually running in production, see "Replacement policy" above instead.

A video is skipped only when a summary already exists with `status: "ok"`
and its `source_transcript_hash`, `model`, and `prompt_version` all still
match. The hash covers the **complete** transcript body (captions), not the
Markdown file's frontmatter and not just the portion actually sent to the
model when truncated (see below) - so a transcript re-fetched with
identical captions doesn't trigger a wasteful re-summary just because
`fetched_at` changed, but a real change anywhere in the transcript is never
invisible. Changing `SUMMARY_MODEL`, or a future prompt revision (which
bumps the `PROMPT_VERSION` constant in `app/summarize.py`), deliberately
re-summarizes everything, resetting the attempt count below since it's a
new unit of work.

A failure isn't retried unconditionally forever, and retrying doesn't wait
for a future run. Each failure is classified `retryable` or not: rate
limits, connection errors, transient malformed output, and failed point
validation are retryable; a safety refusal is not, since it's deterministic
for the same input and retrying just burns budget for a guaranteed repeat.
A retryable failure is retried immediately, in the same call to
`summarize_eligible()`, up to `SUMMARY_MAX_ATTEMPTS_PER_VIDEO` (default
`3`) attempts in one pass - there's no waiting between attempts except a
short fixed pause (`_RATE_LIMIT_RETRY_DELAY_SECONDS`, 5 seconds) before an
attempt that follows a rate limit specifically, since an immediate
zero-delay retry right after a 429 tends to prolong the throttling rather
than resolve it. Every other retryable failure (a transient connection
blip, a citation that failed grounding) retries at once. A run's
`SummaryReport` (logged by `discover_and_process.py`) includes a `retried`
count - videos this run whose first attempt wasn't its only one - separate
from the `failed` count of this run's own final outcomes.

Only once every immediate attempt is exhausted (or a non-retryable failure
ends the loop early) does a video fall through to the rare, genuinely
deferred outcome: a `status: "error"` artifact recording `attempts` and
`next_retry_at` (`generated_at` + `SUMMARY_RETRY_BACKOFF_SECONDS`, default
`900`), not eligible again until that time passes. This is meant as a last
resort for a persistent provider-level problem that survives every
immediate attempt in this run - it is no longer the normal path a
transient failure takes to eventually succeed. A non-retryable failure has
no `next_retry_at` at all, since it isn't retried regardless of elapsed
time.

**Legacy summary-store fallback behavior (not used by `summarize_backlog.py`).** Some speakers -
meandering, conversational, non-linear delivery - make the per-point
timestamp citation genuinely hard to ground even when the model clearly
understood the content; a video like that can otherwise exhaust its retry
budget and sit permanently as `status: "error"` with no usable output at
all. Once a video's immediate attempts are exhausted and the last failure
is retryable, one extra call is made for a much simpler ask - a plain 2-3
paragraph prose summary of the video as a whole, with no per-line citation
to get wrong. If that succeeds, the artifact is written as `status: "ok"`
with an empty `points` list, `"fallback_summary": true`, and the summary
text itself prefixed with a `⚠️` marker so it reads as visually distinct
anywhere it's displayed, not just via that field. A non-retryable failure
(e.g. a safety refusal) skips the fallback attempt entirely - the simpler
prompt would very likely be refused for the same reason and isn't worth
the extra call. If the fallback call itself fails, the video falls through
to the normal `status: "error"` artifact exactly as it would have
otherwise - this is a best-effort extra attempt, not a guarantee every
video eventually gets a summary.

A genuine, still-broken auth/credential problem is handled differently
from a per-video failure, deliberately - two ways:

- If `ANTHROPIC_API_KEY` isn't set at all, the whole summarization stage
  is skipped cleanly before touching any video or making any API call
  (see Setup above) - this stage is optional, and an unconfigured
  deployment shouldn't fail discover_and_process.py's entire run over it.
- If the key is set but invalid/expired/lacking access, that's detected as
  a side effect of the real pre-flight token count (see Cost controls
  below) *before* a given video's attempt count is touched or anything is
  written for it, and aborts the whole run immediately instead of writing
  a `status: "error"` artifact. This matters because a credential problem
  isn't a property of any one video's content - if it were recorded as a
  non-retryable per-video failure the normal way, fixing the credential
  afterward wouldn't un-poison it, since nothing about that video's own
  transcript hash, model, or prompt version changed.

A transient failure on that same pre-flight token count (a rate limit,
connection error, or 5xx - as opposed to a genuine credential problem) is
*not* treated this way: it's recorded as an ordinary per-video failure
like any other, since aborting the whole run over a momentary blip would
otherwise skip every remaining video in the backlog with no durable retry
state to show for it.

### Transcript length policy

If a transcript's complete body exceeds `SUMMARY_MAX_TRANSCRIPT_CHARS`
(default `400000`, comfortably covering a multi-hour video), only the first
`SUMMARY_MAX_TRANSCRIPT_CHARS` of it is sent to the model, and the
resulting artifact is flagged `"transcript_truncated": true`. Points and
timestamps within the retained (beginning) portion stay accurate; content
past the cutoff simply isn't covered - and the idempotency hash above still
covers the complete, untruncated body, so a change past the cutoff still
triggers re-summarization rather than looking unchanged. This is a
deliberate v1 simplification - explicit, visible truncation rather than
multi-pass chunking-and-merging across several model calls.

### Cost controls

`SUMMARY_MAX_TOTAL_TOKENS_PER_RUN` and `SUMMARY_MAX_COST_USD_PER_RUN` each
independently bound one run - there's no separate cap on the *number* of
videos processed; a run keeps draining the eligible backlog until it's
empty or one of these real spend limits is actually hit. Before
each call, its worst-case cost/tokens are reserved against these caps -
not just checked against totals from already-completed calls - so a
single call starting just under a cap can't push the run well past it.
The input side of that reservation is a real pre-flight count via
Anthropic's token-counting endpoint (covering the system prompt and
output schema overhead, not just the transcript itself), not a
chars-per-token guess - a heuristic like that both undercounts (excludes
the prompt/schema) and isn't a true upper bound either way. The output
side still treats `SUMMARY_MAX_OUTPUT_TOKENS` as fully consumed, the real
worst case. Two distinct outcomes follow from this reservation:

- If a video's own worst-case cost/tokens exceed the **entire** configured
  cap by itself (even from a completely fresh run), it's skipped - logged
  as exceeding the per-run budget alone - rather than treated as "stopped
  on budget," which would otherwise leave it first in line and
  permanently block every other eligible video behind it, every run.
- If the run's cumulative totals so far don't leave enough *remaining*
  headroom for this video's worst case, the run stops here (logged as
  "stopped early on a per-run budget"), leaving it and the rest of the
  backlog for the next scheduled run.

`SUMMARY_MODEL` must have a pricing entry in `app/summarize.py`'s
`PRICING_PER_MTOK_USD` - the app refuses to start otherwise, since an
unrecognized model would make cost estimation silently return `None` and
disable `SUMMARY_MAX_COST_USD_PER_RUN` entirely. A failure that still
received a billed response from the API (a safety refusal, or output that
failed to parse/validate) has its usage counted against these caps too,
even though it's recorded as `status: "error"`. The one case where real
usage isn't accessible at all - the SDK's own schema-validation step
raising before we get access to the raw response - is conservatively
charged this call's own reserved worst-case estimate instead of
contributing zero, since a response plausibly still happened and was
billed; only a failure before any response was ever returned at all (a
connection error, for instance) contributes no usage, since none is known
to have been billed.

### Concurrency

Runs inside the same serialized `discover_and_process.py` job as discovery
and queue processing, for the same reason batching does (see the
concurrency invariant above): `_index.json` isn't safe for a second,
independent writer. The advisory lock is renewed twice per video: once
before the (possibly slow) model call, so a long-running lease doesn't go
stale purely from provider latency, and once again immediately before the
resulting artifact is written to Drive - the second renewal is what
actually matters, since it's the last chance to detect that a concurrent
run has taken over the lock before this run would otherwise write under a
lease it no longer holds.

Every Anthropic API call (the model call and the token-counting pre-flight
check) disables the SDK's own automatic retry/backoff
(`Anthropic(max_retries=0)`). Left at its default, a single call could
silently retry internally for up to ~30 minutes (2 retries, each up to a
10-minute timeout) - comfortably long enough to run past
`DISCOVERY_LOCK_TTL_SECONDS`'s default 30-minute window with no chance to
renew the lock in between. The app's own outer retry loop
(`SUMMARY_MAX_ATTEMPTS_PER_VIDEO`, retried immediately within the same run,
with the lock renewed before each attempt) is the sole retry authority
instead - the same fix already applied to the Webshare proxy's internal
retries in `app/youtube.py`.

## Which summary builder to use

This repo has **two separate summary builders**. They are not versions of
each other - pick by what you want out:

| | **Short summary builder** | **Complex summary builder (dossier)** |
|---|---|---|
| Script | `run_single_video_ollama.py` (local) / `summarize_backlog.py` (Railway, Claude) | `run_video_dossier_ollama.py` (local only) |
| Output | `summary_<id>_<model>.json` - a 1-3 sentence summary, a `video_type`, and up to 20 timestamped major/minor points | `dossier_<id>_<model>.json` **+** `dossier_<id>_<model>.html` - an archive page modelled on [zizek.org/talk/*](https://zizek.org/talk/a-plea-for-bureaucratic-socialism) |
| Contents | summary + points | prose summary, tags, located-moment timeline, ideas, stories, works, people, what it argues, moves, claims, verbatim quotes, related videos, paragraphed transcript |
| Model calls | 1 per chunk (+1 synthesis if chunked) | 2 per chunk (moments, references) + 1 synthesis |
| Time (15-min video, mistral-small, 12GB GPU) | ~3-5 min | ~7-8 min |
| Use for | the feed / dashboard / quick triage | a deep, browsable write-up of one video |

Use the **short builder** for anything feeding the discovery feed, Drive
`summaries/`, or the Streamlit dashboard - that's the format those read.
Use the **complex builder** when you want a full dossier page for a
specific video. The dossier output is not read by the dashboard.

### Running the complex summary builder

> **Status (2026-10-15): only tested on one short video.** The single
> verified run is *How To Play: Alien RPG* (`EgcrLw1IoSQ`, 15:31), which fit
> in **one chunk** - so the long-video path (multi-chunk extraction → merge/
> dedupe → cross-chunk synthesis), checkpoint resume across chunks, and the
> memory behaviour on a long run have **not been exercised yet**. Neither
> have the newer fixes (timestamp cleaning, `repeat_penalty`, render-time
> sentence de-duplication) on a fresh multi-chunk extraction. Run a 40+
> minute video before relying on it for long-form content.

```
python run_video_dossier_ollama.py <video_url_or_id> [model]   # default model: mistral-small
python run_video_dossier_ollama.py --render <dossier_json>      # re-render HTML only, no model calls
```

- **Long videos** are handled the same way as the short builder: the
  transcript is split into chunks (`MAX_CHUNK_CHARS = 20000`, smaller than
  the short builder's 30000 because each chunk gets two extraction passes),
  each chunk is extracted separately, results are merged/deduped, and a final
  synthesis call writes one summary across all chunks.
- **Resumable:** every model call's raw output is cached in
  `dossier_<id>_<model>.calls.json`. A run that gets killed resumes from the
  last finished call; re-running a finished video re-applies grounding and
  rendering with no new model calls. Delete the file (or one key in it) to
  force fresh extraction.
- **Grounding:** a timestamp is only shown if its cited transcript line
  passes the same anchor gate as the short builder (`_resolve_points()`),
  falling back to where the cited verbatim words actually appear. Quotes,
  moves and story pull-quotes must be found word-for-word in the transcript
  or they're dropped.
- **Memory:** mistral-small (~17GB) only part-fits a 12GB card and spills
  ~7GB into system RAM. Run it in a foreground terminal - Claude Code kills
  *background* shells under memory pressure. Chunking bounds context size,
  not model size.
- **"Related"** compares against every other `dossier_*.json` in the same
  folder (shared tags/ideas/people/works).
- Outputs (`dossier_*.json`, `dossier_*.html`) are gitignored, like
  `summary_*`.

## Insight dashboard (optional)

`vidproc_app.py` is a read-only Streamlit dashboard over the summary
archive - browse collected videos by group (Finance/Google, driven by each
channel's `group` field in `channels.json`) and channel, then open one to
read its generated summary and timestamped points. It's deliberately
**public and unauthenticated** (unlike the FastAPI service's `X-API-Key`
gate), since it's meant to be a publicly viewable dashboard.

### Run locally

```bash
pip install -r requirements-vidproc.txt   # separate from requirements.txt - see below
export DRIVE_FOLDER_ID=... GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... GOOGLE_OAUTH_REFRESH_TOKEN=...
streamlit run vidproc_app.py
```

Uses its own `requirements-vidproc.txt`, not the main `requirements.txt` -
streamlit's Starlette-based server needs a newer `starlette` than
`fastapi==0.115.6` (in `requirements.txt`) allows in the same environment;
installing both together resolves to a `starlette` too old for streamlit
and it fails at import time. `vidproc_app.py`'s own import chain never
touches fastapi/starlette/uvicorn/APScheduler, so there's no reason to
force them into the same resolution. The deployed service
(`Dockerfile.vidproc`) keeps the same isolation for the same reason.

Reuses the same `DRIVE_FOLDER_ID` and Google OAuth credentials as the rest
of the app (read-only) - no separate setup. If those aren't set, or Drive
access fails, the app renders a generic "temporarily unavailable" page
rather than an error page, a stack trace, or any credential/Drive detail -
this is the public-facing failure state, not a bug.

### How it reads data

The feed itself is assembled read-only from the same three Drive-hosted
sources the pipeline already writes - `channels.json`, `_index.json`, and
each video's `summaries/<video_id>.json` - via `app/insights_store.py`. No
new Drive capability was needed: `_index.json` already enumerates every
video ever attempted, so the dashboard never lists a Drive folder
directly. A video only appears once it has a `status: "ok"` summary
artifact; a video that's never been summarized, or whose summarization
recorded `status: "error"`, is counted in a small "N pending" note in the
header rather than shown as a broken feed item.

The one exception is the optional Admin tab below, which writes to
`channels.json`.

### Admin panel (optional)

Set `VIDPROC_ADMIN_TOKEN` to enable an **Admin** tab alongside the
group tabs, with a form to register a new channel (channel ID, display
name, enabled, group, languages) without editing `channels.json`
directly. Unset (the default), the tab doesn't appear at all.

The group field is a selectbox of every group currently in use, plus an
explicit "+ Create a new group..." option - not free text. Creating a
new top-level tab is therefore always a conscious choice: picking an
existing group can never accidentally spawn a new one via a typo (e.g.
"Goggle" vs "Google"), since choosing that option requires separately
typing and confirming the new group's name - and its `video_types` (see
"Per-group video types (groups.json)" above): a new group can't be
created without specifying at least one classification category up
front, so it's never left unconfigured.

This is a shared-secret bearer token compared with a constant-time
check (`secrets.compare_digest`), not a real user/password auth system -
appropriate for a single-operator tool, but the entry form has no
rate-limiting or lockout, so generate a long random value rather than a
memorable password:

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Unlocking sets a per-session flag only (`st.session_state`, no cookie or
persistent token) - a hard page reload opens a fresh Streamlit session
and requires the token again.

A channel ID is validated against YouTube's `UC` + 22-character shape and
preflight-checked against its actual RSS feed (`app/discovery.py`'s
`fetch_channel_feed()`) *before* anything is written - a bad ID is
rejected outright rather than being permanently persisted only to fail
later. Once that passes, the channel is written via
`app/channel_store.py`'s `write_channels()`, then `backfill_new_channels()`
(see "Backfilling a newly added channel" above) attempts an immediate
backfill of its current RSS feed under the same `_discovery_lock.json`
`discover_and_process.py` uses. This never blocks the admin panel,
though: if that lock is already held, the channel is still added
immediately and the panel reports the backfill as deferred to the next
`discover_and_process.py` run, rather than waiting for the lock to free
up. A backfill that fails outright (as opposed to being deferred) is
reported as "channel added, backfill failed" - distinct from total
failure, since the channel registration itself already succeeded.

Channel grouping is resolved via `channel_id` (see "Transcript
summarization" above) - a video whose `channel_id` doesn't match any
currently configured channel (predates that field, or its channel was
later removed from the registry) falls back to the **Finance** group and
appears there under an **"Unassigned / Other"** pseudo-channel in the
channel filter, rather than a separate top-level tab, so group tabs stay
purely driven by `channels.json` membership. Run
`python backfill_channel_ids.py` once to backfill `channel_id` onto
already-archived summaries from before that field existed (see the script
for details/limitations).

Minor insight points are shown alongside major ones by default (major
points bold, minor visually de-emphasized) with a "Show minor points"
toggle in the detail view to hide them - no point is ever permanently
hidden.

### Deploying the insight dashboard

Runs as its own Railway service using `railway.vidproc.toml`, following
the same multi-service-per-repo pattern already used for
`railway.discover-and-process.toml` - each Railway service in the project
picks a different `railway.*.toml` as its config file. Unlike the other
two services, this one builds from a dedicated `Dockerfile.vidproc`
(`builder = "DOCKERFILE"`) instead of Railway's default Nixpacks
auto-detection, installing only `requirements-vidproc.txt` - Nixpacks
would otherwise auto-install the repo-root `requirements.txt`
(fastapi/uvicorn/starlette) into the same environment as streamlit,
which conflicts (see "Insight dashboard" → "Run locally" above).

**Note on the deployment target:** issue #8 originally asked for
`moopertonic.net/vidproc` (an apex-domain path, via a Cloudflare Worker
path-proxy). This deploys as a **subdomain**, `vidproc.moopertonic.net`,
instead - the same pattern already used in production for
`oil.moopertonic.net` (`OwenTanzer/oil-futures`, a Cloudflare-proxied
CNAME straight to a Railway custom domain, no Worker). Simpler, and
consistent with existing infrastructure.

1. **Railway service.** In the existing Railway project, add a new
   service pointed at this repo/branch, with `railway.vidproc.toml` as its
   config file. Set its environment variables: `DRIVE_FOLDER_ID`,
   `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` /
   `GOOGLE_OAUTH_REFRESH_TOKEN` (the same read-only Drive credentials as
   the main service), and optionally `VIDPROC_CACHE_TTL_SECONDS`.
   **Do not set `API_KEY`** - this service has no gate. Deploy, and
   confirm it boots correctly on its default `*.up.railway.app` domain
   before touching DNS.

2. **Custom domain.** In the new service's Railway settings → Networking →
   Custom Domain, add `vidproc.moopertonic.net`. Railway generates a
   target CNAME value specific to this binding.

3. **Cloudflare DNS.** In the `moopertonic.net` zone, add a CNAME record:
   Name `vidproc`, Target the value from step 2, Proxy status **Proxied**
   - mirroring `oil.moopertonic.net`'s existing record (check its actual
   TTL/proxy settings in the live zone first, to genuinely match it rather
   than assume).

4. Wait for DNS propagation and Railway's automatic TLS issuance, then
   confirm `https://vidproc.moopertonic.net` serves the app correctly.

**Rollback:** remove the Cloudflare CNAME record for `vidproc`, and remove
(or leave unbound) the custom domain in the Railway service's settings.
Neither step touches `railway.toml`, the main FastAPI service, or any
other DNS record in the zone - this is an entirely separate service plus
one additive DNS record, isolated by construction.

**Deployed smoke test** (manual checklist - run once against
`vidproc.moopertonic.net` after DNS/TLS settle):

1. Direct navigation loads the header, tabs, and a populated (or cleanly
   empty) feed.
2. Hard refresh reloads correctly, no stale/broken state.
3. Switching group tabs updates the feed and resets the channel filter to
   "All channels" for the new group.
4. Selecting individual channels vs. "All channels" narrows the feed
   correctly, interactively (no full page reload).
5. Opening a headline shows points in timestamp order; "Back to feed"
   returns to the same group/channel scope.
6. A point's timestamp link opens the source video anchored near the
   right time.
7. The Drive-transcript link (where present) opens a valid, view-only
   link.
8. The unavailable-service state is verified **locally** (temporarily
   unset `DRIVE_FOLDER_ID` and confirm the clean, generic message with no
   credential/stack-trace detail) - not tested against live prod, since
   prod shouldn't be intentionally broken.

## Project layout

```
app/
  main.py          FastAPI app: /healthz, /transcripts, /batch/run
  pipeline.py       process_video(): the shared fetch-and-archive path
  batch.py          run_batch(): queue-driven or explicit-list batch runs
  youtube.py        URL parsing, transcript fetch, oEmbed title lookup, Markdown rendering
  drive.py          Drive upload/read + generic Drive file helpers
  queue_store.py    queue.json read/write helpers (plain URLs or {"url","languages","first_seen_at"} entries)
  channel_store.py  channels.json read/write helper (the discovery source registry)
  group_store.py    groups.json read/write helper (each group's video_type classification categories)
  discovery.py      RSS feed fetch/parse + discover_and_enqueue(): queues unseen uploads
  job_lock.py       Drive-based advisory lock preventing overlapping discovery runs
  summarize.py      Claude model call: transcript -> structured, schema-validated summary
  summary_store.py  summaries/<video_id>.json read/write, idempotency, and summarize_eligible()
  insights_store.py  read-only data layer for the Streamlit dashboard: load_snapshot()
  scheduler.py       optional in-process APScheduler wiring
  config.py         environment variable loading/validation
  models.py         request/response schemas
vidproc_app.py      Streamlit dashboard entrypoint (streamlit run vidproc_app.py)
vidproc/
  styling.py         CSS/color constants for the dashboard's visual framework
  state.py           pure group/channel-filter/sort logic, no Streamlit import
  render.py          feed-card and detail-view rendering
requirements-vidproc.txt  separate, minimal dependency set for the dashboard - see "Insight dashboard" above
Dockerfile.vidproc  dedicated build for the vidproc Railway service (isolated from requirements.txt)
batch_runner.py     standalone entrypoint for a separate Railway Cron Job service
discover_and_process.py  standalone entrypoint: discover -> process queue (locked)
summarize_backlog.py     standalone entrypoint: concurrent independent summary worker
backfill_channel_ids.py  one-off script: recover channel_id for pre-existing summaries
backfill_new_channels.py  one-off/rerunnable script: backfill a newly-added channel's current RSS feed, decoupled from discover_and_process.py's lock
get_refresh_token.py  one-time local script to mint the Drive OAuth refresh token
run_single_video_ollama.py  SHORT summary builder, local: one video -> summary + timestamped points (Ollama, default mistral-small)
run_single_video_groq.py    SHORT summary builder via Groq (chunked + rate-paced)
run_video_dossier_ollama.py COMPLEX summary builder, local: one video -> archive-style dossier JSON + HTML page
```
