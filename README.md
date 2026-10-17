# Clipy

Clipy runs YouTube clip channels without a person in the loop. It takes
long-form videos such as talks, lectures, podcasts, interviews and streams,
cuts them into standalone clips of 8 to 20 minutes that each feature one
speaker, and publishes them. Each channel has one goal: ad revenue, or watch
hours if it is not monetized yet.

| Channel | Who appears | Goal |
|---|---|---|
| [Jonathan Blow Clips](https://www.youtube.com/@JonathanBlowClips) (`blow`) | Jonathan Blow only | Ad revenue |
| [Casey Muratori Clips](https://www.youtube.com/@caseymuratoriclips) (`casey`) | Casey Muratori only | Watch hours |
| [George Hotz Clips](https://www.youtube.com/@georgehotzclips) (`hotz`) | George Hotz only | Watch hours |
| [Science Clips Daily](https://www.youtube.com/@ScienceClipsDaily) (`science`) | Anyone credible explaining science | Watch hours |
| [Philosophy Clips Daily](https://www.youtube.com/@PhilosophyClipsDaily) (`philosophy`) | Anyone credible on philosophy and big ideas | Watch hours |

## What happens each day

Once a day, an AI agent runs each channel in turn. It reads the channel's
analytics, looks at how recent uploads are doing, and decides what to change.
It finds new source videos, checks that the right person does most of the
talking, and starts clipping them. On Mondays it schedules the coming week's
uploads, one per day. It also rewrites titles and thumbnails on videos that
are underperforming, and learns which excerpts and titles work from the results.

On the Science and Philosophy channels the agent also chooses who to feature.
It tries different speakers and topics to learn what brings in watch hours,
and it finds a photo of each new speaker for the thumbnails.

The agent has no memory of its own between runs. Everything it knows comes
from a notebook it reads at the start of each run and updates at the end: its
strategy, the experiments it is running, what it has learned from the
numbers, and a list of candidate source videos to clip next.

After all channels have run, a second agent reads what each one did and
writes a single report, which is sent to the owner over Matrix.

## How it is built

**The agent** is Codex CLI, signed in with a ChatGPT subscription rather than
an API key, and started by [`agent/run.sh`](agent/run.sh). Its instructions
are [`agent/prompt.md`](agent/prompt.md), the routine every channel follows,
plus a short policy file for the channel in [`agent/channels/`](agent/channels).
The policy says what the channel optimises for, who may appear in it, where
to look for sources, and what the owner has to do by hand. These rules live
only in the prompts; nothing in the code enforces them, so changing how a
channel behaves means editing its policy file. The agent can search the web
and run shell commands inside its container.

**The MCP server** (`clipy-mcp`) gives the agent its tools and does the actual
work. Each channel in `config.toml` gets its own endpoint at
`/mcp/<channel>`, so an agent can only touch its own channel. The tools cover:

- general Google API access driven by searchable official discovery specs,
  plus raw dated counter snapshots and Studio observations
- searching for source videos and checking who speaks in them
- starting clipping and upload jobs, which run in the background
- editing queued clips, rendering artwork, and raw API requests for live videos
- reading and changing reusable editorial guidance
- reading and writing the notebook

The API interface has no per-endpoint wrappers or hand-written metric catalog.
`google_api_spec` searches Google's published discovery documents bundled with
the installed client and returns exact subtrees by JSON Pointer.
`google_api_read` and `google_api_write` invoke the named service method with
its original parameters and return the service response. Read/write separation
comes from each method's published HTTP verb. Existing OAuth scopes are kept.
Media uploads are restricted to files inside the current channel's directory.
Every mutation records intent, measured observations, and outcome under
`channels/<channel>/api-writes/`; failed writes remain unconfirmed until Codex
checks the remote state. No write retries happen automatically.

**Codex owns the editorial workflow end to end.** It starts `prepare_source`
to download and transcribe a source with AssemblyAI, then reads the complete
speaker-labelled, timestamped transcript through `get_source_transcript`
(following every page to the end). The same Codex session that knows the
channel strategy and analytics chooses the excerpts and writes their titles,
descriptions, chapters and thumbnail concepts. It submits those choices to
`process_source`; the server only validates the render contract, cuts with
ffmpeg, draws illustrations with the image service and adds the clips to the
queue. No transcript or clip text is sent to another text model.

Preparation and rendering run as persisted background jobs. Codex records the
unfinished stage in its notebook and resumes it next run. Rendering retries
reuse saved selections; submitting a different selection over existing exports
is rejected to preserve queue identity. Empty selections explicitly record a
source with no worthwhile clips. Jobs from all channels run one at a time.

For manual work, use `clipy prepare CHANNEL URL`, read all pages with
`clipy transcript CHANNEL URL --offset N`, then submit a JSON array matching
the MCP `process_source` schema with `clipy process CHANNEL URL --clips FILE`.
Omit `--clips` only to resume saved selections. Non-fixed channels also need
`--speaker`. These commands do not choose excerpts for you.

The original-video link includes the clip's start timestamp.

Clips are fully re-encoded with aligned audio and video timelines. This avoids
the frame repetition and audio drift possible when splicing copied H.264
onto a re-encoded opening segment. Exports use one continuous encode with aligned timelines.

**Thumbnails** have a picture on the left and, on the white right side, an
illustration of the clip's main idea made by an image model. For Jonathan
Blow the picture is a fixed template, `thumbnails/blow.jpg`. For the other
channels it is a headshot of the speaker, which the agent finds on the web,
crops to a portrait and saves under `headshots/` in the data directory. All
channels share the same headshots.

**The daily report** is one Matrix message covering every channel. Its header
uses channel-level YouTube Studio internal API daily series collected hourly,
showing the latest available reporting day and the last 30 days. Views and
subscriber changes can reach yesterday while earnings are still processing;
each metric keeps its own date. Missing or processing data stays unavailable,
and channel readings more than two hours old are not shown as fresh totals.
These are YouTube reporting dates, with estimates explicitly labelled.
Status earnings are requested from YouTube in AUD and labelled A$; older USD
channel snapshots stay unavailable until the next successful collection.
Below the numbers, the observer writes only tasks the owner must perform,
or “Owner actions: none.” Run footers show input and output tokens without
cached or reasoning token counts.

George Hotz Clips uses the supplied fixed photo template, focuses on AI,
programming and technology, and launches by filling seven future daily slots
before joining the usual Monday batching routine. It runs alongside the
existing channels.

Engagement and revenue are queried separately, with their own freshness
dates. Hourly YouTube Data API snapshots provide current views, likes,
comments and rounded subscriber counts. Codex chooses comparison windows
and computes changes when baselines exist. `read_observations` exposes the
raw dated snapshots separately from delayed watch hours, revenue, impressions and CTR.

Clipy collects fresh Studio analytics hourly with `clipy studio-collect`
using direct authenticated `yta_web/get_cards` requests. It loads cookies from
the private session checkpoint, bootstraps the current Studio client version
and channel delegation through HTTP, and regenerates authentication hashes
for each request. Per-video readings request Since published and USD; channel
daily totals for status reports request AUD, retaining
unrounded totals, provider estimates, missing values and failures. The agent
reads these results through `read_observations`.

Chromium is used for comment pinning, interactive Google sign-in and cookie recovery. Analytics collection
never opens analytics pages or reads DOM cards. On authentication failure it
refreshes the HTTP client context, then reads the browser's live cookie jar
once if necessary. Only a successfully authenticated, channel-checked response
can update the owner-only cookie checkpoint. Response cookie updates persist
between runs, so analytics collection also works while the browser is unavailable.
Google can still require interactive sign-in if it revokes the session.

The browser and collector run on azalab-0 without a Mac or Codex calls. Their
deployment belongs in the homelab repo; the desktop is reached only through
SSH/Kubernetes forwarding and its debugging port remains pod-local.
`CLIPY_STUDIO_SESSION` points to the approved private-volume checkpoint. It
contains account-access credentials, not passwords: keep it out of logs,
Git and ordinary application data. Public browser user-agent metadata is saved
alongside it; client context and delegation variables are refreshed in memory.

Upload success is distinct from processing success. Clipy retains each MP4
until YouTube confirms successful processing, and synchronises queue dates
with YouTube's current calendar. Agents can inspect and repair failed or
stalled private uploads on any day. Recovery fully re-encodes the excerpt,
uploads a private replacement, confirms processing, transfers the future
schedule and then deletes the broken copy. It refuses public/watched videos,
working uploads and policy rejections; retries resume the same replacement.
New release batches still happen on Mondays.

## The one manual step

The owner handles Jonathan Blow monetization in YouTube Studio. The daily
report raises only new issues requiring account access or an owner decision.
The other channels need no per-upload monetization step.

## Production

Delivery uses one host job with a cached Linux test image. It runs the full
E2E suite with four workers, sharing expensive media fixtures on one worker,
then builds and publishes the production image. System packages, Python
dependencies and Chromium are cached separately from source edits.
The job's 300-second budget includes runner queue time, tests, image build,
publishing and readiness of the app, site, collector, comment worker and
agent template. It prints `COMMIT_TO_READY_SECONDS` only after live proof.
`/releasez` exposes only the commit and aggregate readiness, using a
namespace-scoped in-cluster identity. CI receives no Kubernetes credential.
Clipy has its own ten-second Flux image automation, with registry webhooks
and a thirty-second registry-scan fallback.

CI builds the image `ghcr.io/s1dny/clipy` from every `main` commit whose
checks pass, and Flux deploys it to azalab-0. The Kubernetes manifests are in
the homelab repo under `flux/clusters/azalab-0/manifests/apps/clipy`.

The `clipy` Deployment serves the MCP server inside the cluster at
`http://clipy.clipy.svc:8000/mcp/<channel>`. The `clipy-agent` CronJob runs
the agents every day at 04:00 Sydney time.

Everything the bot keeps is on the host under `/srv/clipy`:

- `channels/<channel>/`: each channel's YouTube login, clip queue, notebook
  (`notes.md`), prompt overrides and working files
- `headshots/`: speaker photos shared by all channels
- `codex/`: the agent's ChatGPT login
- the background job list and the Matrix room

To run the agents now instead of waiting for the schedule:

```bash
ssh azalab-0 kubectl -n clipy create job --from=cronjob/clipy-agent agent-manual-$(date +%s)
```

Its output is in `kubectl -n clipy logs job/<name>`.

The agent needs to be signed in to ChatGPT once, and again if the login ever
expires. This uses a device code, so the server's session stays separate from
your own:

```bash
ssh -t azalab-0 "kubectl -n clipy exec -it deploy/clipy -- sh -c 'mkdir -p /data/codex && CODEX_HOME=/data/codex exec codex login --device-auth'"
```

## Configuration

`config.toml` holds every setting that is not a secret: which transcription and image models to use,
and for each channel its handle, YouTube category, speaker, thumbnail
template and prompt templates. Settings under `[defaults]` apply to every
channel, and `[channels.<name>]` overrides them. A channel with a `speaker` is
about that one person. A channel without a `thumbnail_template` uses speaker
headshots. Start by copying `config.toml.example`.

The agent can change its editorial guidance and the image/description templates while it runs. Its versions
are saved in the channel's `prompt_overrides.json` and take priority over
`config.toml`.

| Variable | Used by | Meaning |
|---|---|---|
| `ASSEMBLYAI_API_KEY` | pipeline | Transcription |
| `OPENROUTER_API_KEY` | pipeline | Thumbnail illustrations only |
| `CLIPY_MCP_TOKEN` | server, agent | Password for the MCP server, at least 32 characters |
| `CLIPY_DATA_DIR` | everything | Where channel data, headshots and bot state live (`/data` in the image) |
| `CLIPY_CONFIG` | server, CLI | Path to `config.toml` (defaults to the one in the repo) |
| `CLIPY_TIMEZONE` | server | Timezone for notebook entries (default `Australia/Sydney`) |
| `YOUTUBE_CLIENT_SECRETS` | `clipy auth` | Google OAuth client file (default `client_secrets.json`) |
| `CODEX_MODEL`, `CODEX_REASONING` | agent | Model and reasoning effort (default `gpt-6.1-sol`, `medium`) |
| `CLIPY_AGENT_NOTE` | agent | A one-off instruction added to every channel's prompt for a manual run |
| `CLIPY_QUOTA_RETRY_SECONDS` | agent | Delay before retrying a Codex usage-limit failure that ran no tools (default 900 seconds, at most three attempts) |
| `MATRIX_ACCESS_TOKEN` | agent | The report bot's Matrix token; without it no report is sent |
| `MATRIX_NOTIFY_USER` | agent | Who the bot invites to its report room the first time |
| `MATRIX_HOMESERVER` | agent | Matrix homeserver URL (defaults to the one in the cluster) |

For local use, put secrets in `.env` (see `.env.example`).

### Connecting a YouTube channel

In the Google Cloud project, enable the YouTube Data API v3, the YouTube
Analytics API and the YouTube Reporting API. Create an OAuth client of type
Desktop app and save it as `client_secrets.json`. Keep the OAuth app set to
"In production", because in "Testing" its logins expire after 7 days.

Then sign each channel in:

```bash
clipy auth <channel>
```

This prints a Google link. Open it on the same machine, pick the channel's
brand account and allow access. The login is saved to
`channels/<channel>/.youtube_token.json`, but only if it belongs to the
channel's `handle`. Copy it to the server with:

```bash
ssh azalab-0 "kubectl -n clipy exec -i deploy/clipy -- sh -c 'mkdir -p /data/channels/<channel> && cat > /data/channels/<channel>/.youtube_token.json && chmod 600 /data/channels/<channel>/.youtube_token.json'" < channels/<channel>/.youtube_token.json
```

## Running it locally

Clipy needs Python 3.14 and ffmpeg. Install it with `uv sync`, or with
`uv tool install --editable .` to get a `clipy` command. To clip one video by
hand:

```bash
clipy process <channel> <youtube-url> [--speaker "Full Name"]
```

## Development

```bash
uv run ruff format .
uv run ruff check .
uv run playwright install chromium
uv run pytest
```

The suite runs complete CLI, MCP and browser workflows against local HTTP
providers, with real ffmpeg exports and persisted queues. It covers source
resume, channel isolation, interrupted uploads, private-upload repair,
analytics collection, comment recovery, daily-agent retries and rollout
readiness. Pytest uses four workers by default; CI enforces a 60-second limit.
