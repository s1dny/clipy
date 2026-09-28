# Clipy

Clipy is a fully autonomous YouTube automation bot. It runs clip channels on
its own, turning long-form videos (talks, lectures, podcasts, interviews,
streams) into standalone clips of one speaker each, with one goal per channel:
ad revenue, or watch hours for a channel that isn't monetized yet.

It runs three channels:

| Channel | Speakers | Goal |
|---|---|---|
| [Jonathan Blow Clips](https://www.youtube.com/@JonathanBlowClips) (`blow`) | Jonathan Blow | Ad revenue |
| [Science Clips Daily](https://www.youtube.com/@ScienceClipsDaily) (`science`) | Anyone credible explaining science | Watch hours |
| [Philosophy Clips Daily](https://www.youtube.com/@PhilosophyClipsDaily) (`philosophy`) | Anyone credible on philosophy and big ideas | Watch hours |

Every day an AI agent runs each channel in turn: it reads the channel's
analytics, decides what to change, finds and clips new source videos,
schedules the weekly uploads, retitles and re-thumbnails underperforming
videos, keeps a strategy notebook, and sends the owner a report over Matrix.
On the open channels it also chooses the speakers, experimenting to learn
which speakers and topics bring watch hours, and finds each new speaker's
headshot for the thumbnails. The only human step is turning on monetization
for a monetized channel's weekly uploads in YouTube Studio, which the YouTube
API cannot do.

## How it works

```
  Codex (ChatGPT login)  ── MCP /mcp/<channel> ──▶  clipy-mcp  ──▶  YouTube Data,
  agent/run.sh, daily,                               (tools,          Analytics and
  one run per channel                                 jobs)            Reporting APIs
        │                                              │
        ▼                                              ▼
  Matrix report per channel             pipeline: yt-dlp → AssemblyAI → LLM clip
  ("clipy status" room)                 picks, summaries, chapters → ffmpeg →
                                        thumbnails → upload queue
```

- **The agent** is Codex CLI (`codex exec`) signed in with a ChatGPT
  subscription, not an API key. Its prompt is
  [`agent/prompt.md`](agent/prompt.md), the daily routine shared by every
  channel, followed by the channel's policy in
  [`agent/channels/<channel>.md`](agent/channels): its goal, who may speak,
  where sources come from, how to experiment, and what the owner has to do.
  Changing a policy means editing the prompt; the server enforces none of it.
  Codex has live web search and runs commands without its own sandbox; the
  agent pod is the sandbox, with the data directory mounted read-only except
  for the Codex login and the shared headshots.
- **The MCP server** (`clipy-mcp`) is mechanism only, and serves every
  channel in `config.toml` at `/mcp/<channel>`. Its tools cover analytics
  (ranked by revenue or watch hours), thumbnail impressions and click-through
  (from the YouTube Reporting API's daily reach reports), source discovery and
  speaker checks, background clipping and upload jobs, clip and live-video
  editing, prompt overrides, a strategy notebook that is the agent's memory
  between runs, and the Matrix report.
- **The pipeline** downloads a source, transcribes it with speaker labels,
  has an LLM pick 8-20 minute clips of the source's main speaker, writes each
  clip a summary and YouTube chapters, cuts them with ffmpeg, draws
  thumbnails, and queues them. Every step is cached, so an interrupted job
  resumes. Jobs from all channels run one at a time.
- **Thumbnails** have a picture on the left and, on the white right side, an
  illustration of the clip's idea from the image model. The picture is the
  channel's fixed template (`thumbnails/blow.jpg`) or, on the open channels,
  the clip speaker's headshot. The agent finds each headshot with web search,
  crops it to a 2:3 portrait with Python, and saves it to
  `headshots/<speaker-name>.jpg` in the data directory, where every channel
  shares it.
- **The report**: the agent sends one Markdown report per channel a day with
  the `send_report` tool (the goal metric, top videos, changes, anything to
  do), headed with the channel's handle. When the run ends,
  [`agent/run.sh`](agent/run.sh) edits that message to append a stats line
  with the run's duration, input and output tokens, and tool calls. A failed
  run sends the failure instead.

## Weekly human step

For a monetized channel, the agent schedules the coming week's videos on the
batch day and lists them in its report. In YouTube Studio, open Content,
select the scheduled videos, then Edit, Monetization: On, before the first one
goes live. The unmonetized channels need nothing.

## Production

The image `ghcr.io/s1dny/clipy` is built by CI from each green `main` commit
and rolled out by Flux on azalab-0; see `flux/clusters/azalab-0/manifests/apps/clipy`
in the homelab repo. The `clipy` Deployment serves the MCP server in-cluster
at `http://clipy.clipy.svc:8000/mcp/<channel>`, and the `clipy-agent` CronJob
runs the agent daily at 04:00 AEST for each channel in turn. State lives on
the host at `/srv/clipy`: channel data under `channels/<channel>/`, speaker
headshots under `headshots/`, the job list, the agent's Codex login under
`codex/`, and the Matrix room.

Run the agent now:

```bash
ssh azalab-0 kubectl -n clipy create job --from=cronjob/clipy-agent agent-manual-$(date +%s)
```

Logs are in `kubectl -n clipy logs job/<name>`. Sign the agent into ChatGPT
(once, or if its login ever expires) with a device code, which keeps the
server's session separate from your laptop's:

```bash
ssh -t azalab-0 "kubectl -n clipy exec -it deploy/clipy -- sh -c 'mkdir -p /data/codex && CODEX_HOME=/data/codex exec codex login --device-auth'"
```

## Configuration

`config.toml` holds all non-secret settings: models, and each channel's
handle, YouTube category, speaker, thumbnail template and prompt templates
(`[channels.<name>]` overrides `[defaults]`). A channel with a `speaker` is
about that one person; without a `thumbnail_template`, its thumbnails use
headshots. Copy `config.toml.example` to start. The agent can override the
three prompt templates at runtime with the `update_prompts` tool; overrides
live in the channel's `prompt_overrides.json`.

| Variable | Used by | Meaning |
|---|---|---|
| `ASSEMBLYAI_API_KEY` | pipeline | Transcription |
| `OPENROUTER_API_KEY` | pipeline | Clip picking, summaries, thumbnails |
| `CLIPY_MCP_TOKEN` | server, agent | Bearer token for the MCP server, 32+ characters |
| `CLIPY_DATA_DIR` | everything | Holds `channels/`, `headshots/` and bot state (default `/data` in the image) |
| `CLIPY_CONFIG` | server, CLI | Path to `config.toml` (default: the repo's) |
| `CLIPY_TIMEZONE` | server | Timezone for notebook timestamps (default `Australia/Sydney`) |
| `YOUTUBE_CLIENT_SECRETS` | `clipy auth` | OAuth client file (default `client_secrets.json`) |
| `CODEX_MODEL`, `CODEX_REASONING` | agent | Model and effort (default `gpt-6-sol`, `medium`) |
| `MATRIX_ACCESS_TOKEN` | server, agent | The report bot's token; no reports without it |
| `MATRIX_NOTIFY_USER` | server | Who the bot opens its report room with |
| `MATRIX_HOMESERVER` | server, agent | Homeserver URL (default: the in-cluster tuwunel) |

Secrets for local use go in `.env` (see `.env.example`).

### YouTube access

In the Google Cloud project, enable the YouTube Data API v3, YouTube Analytics
API and YouTube Reporting API, create an OAuth client (Desktop app) saved as
`client_secrets.json`, and keep the OAuth app "In production" (a "Testing"
app's refresh tokens expire after 7 days). Then sign each channel in:

```bash
clipy auth <channel>
```

It prints a Google link; open it on the same machine, pick the channel's
brand account, and allow access. The token is saved to
`channels/<channel>/.youtube_token.json` only if it belongs to the channel's
`handle`. Copy it to the server with:

```bash
ssh azalab-0 "kubectl -n clipy exec -i deploy/clipy -- sh -c 'mkdir -p /data/channels/<channel> && cat > /data/channels/<channel>/.youtube_token.json && chmod 600 /data/channels/<channel>/.youtube_token.json'" < channels/<channel>/.youtube_token.json
```

## Local use

Install with `uv sync` (or `uv tool install --editable .` for a `clipy`
command). Requires Python 3.14 and ffmpeg.

```bash
clipy process <channel> <youtube-url> [--speaker "Full Name"]
```

## Development

```bash
uv run ruff format .
uv run ruff check .
uv run pytest tests   # boots the MCP server and drives it end to end
```

## Layout

```
agent/
  prompt.md          The daily routine every channel's agent follows
  channels/          Each channel's policy, appended to the routine
  run.sh             The daily runs: codex exec per channel, stats, report follow-up
  events.py          Reads codex's JSON events: live log and the stats line
clipy/
  mcp_server.py      MCP tools the agent uses, served per channel
  channel.py         A channel's directory, config and YouTube account
  analytics.py       Channel videos and performance reports
  sources.py         Finding and vetting source videos
  pipeline.py        Source video to queued clips
  jobs.py            Background job runner (clipping, uploads)
  cli.py             clipy auth and clipy process
  download.py        yt-dlp downloads of the separate audio and video streams
  transcription.py   AssemblyAI transcription with speaker labels
  clips.py           LLM clip picking, summaries and chapters
  cutting.py         Frame-accurate smart cuts with ffmpeg
  thumbnails.py      Illustration generation and thumbnail compositing
  headshots.py       Speaker headshots for the thumbnails
  upload.py          YouTube auth and uploads
  reach.py           Impressions and click-through from reach reports
  matrix.py          The daily Matrix reports
  queue.py           A channel's clip queue
  config.py, common.py, logging_config.py
tests/               End-to-end tests against the running server
thumbnails/          Fixed thumbnail templates
```
