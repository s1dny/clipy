# Clipy

Clipy is a fully autonomous YouTube automation bot. It runs the
[Jonathan Blow Clips](https://www.youtube.com/@JonathanBlowClips) channel on
its own, with one goal: maximise YouTube ad revenue minus what it spends on
APIs.

Every day an AI agent reads the channel's analytics, decides what to change,
finds and clips new source videos, schedules uploads, retitles and
re-thumbnails underperforming videos, keeps a strategy notebook, and sends a
report over Matrix. The only human step is turning on monetization for each
week's uploads in YouTube Studio, which the YouTube API cannot do.

## How it works

```
  Codex (ChatGPT login)  ── MCP, bearer token ──▶  clipy-mcp  ──▶  YouTube Data,
  agent/run.sh, daily                               (tools,          Analytics and
  at 04:00 AEST                                      jobs)            Reporting APIs
        │                                              │
        ▼                                              ▼
  Matrix report                         pipeline: yt-dlp → AssemblyAI → LLM clip
  ("clipy status" room)                 picks, summaries, chapters → ffmpeg →
                                        thumbnails → upload queue
```

- **The agent** is Codex CLI (`codex exec`) signed in with a ChatGPT
  subscription, not an API key. [`agent/prompt.md`](agent/prompt.md) holds the
  whole channel policy: Jonathan Blow must be the main speaker, only original
  long-form sources, one video goes live per day, uploads happen in a weekly
  batch on Mondays (Sydney time) as private videos scheduled 12 hours to 8 days
  ahead, and the daily review and tuning routine. Changing the policy means
  editing the prompt; the server enforces none of it.
- **The MCP server** (`clipy-mcp`) is mechanism only. Its 20 tools cover
  analytics and revenue (profit is revenue minus the API spend in
  `costs.jsonl`), thumbnail impressions and click-through (from the YouTube
  Reporting API's daily reach reports), source discovery and speaker checks,
  background clipping and upload jobs, clip and live-video editing, prompt
  overrides, and a strategy notebook that is the agent's memory between runs.
- **The pipeline** downloads a source, transcribes it with speaker labels,
  has an LLM pick 8-20 minute clips, writes each clip a summary and YouTube
  chapters, cuts them with ffmpeg, draws thumbnails onto the channel's
  template, and queues them. Every step is cached, so an interrupted job
  resumes.
- **The report**: after each run, [`agent/run.sh`](agent/run.sh) sends the
  agent's one-paragraph summary (earnings, top videos, changes, anything to
  do) to Aiden over Matrix, followed by a stats line with the run's duration,
  input and output tokens, and tool calls. A failed run sends the failure
  instead.

## Weekly human step

Each Monday the agent schedules the coming week's videos. In YouTube Studio,
open Content, select the scheduled videos, then Edit, Monetization: On, before
Tuesday's goes live. The Monday report lists them.

## Production

Clipy runs on azalab-0 from `ghcr.io/s1dny/clipy`, built by CI from each green
`main` commit and rolled out by Flux; see
`flux/clusters/azalab-0/manifests/apps/clipy` in the homelab repo. The
`clipy` Deployment serves the MCP server in-cluster at
`http://clipy.clipy.svc:8000/mcp`, and the `clipy-agent` CronJob runs the
agent daily at 04:00 AEST (`Australia/Brisbane`, so 05:00 in Sydney during
daylight saving). State lives on the host at `/srv/clipy`: channel data under
`channels/<channel>/` and the agent's Codex login and run summaries under
`codex/`.

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

`config.toml` holds all non-secret settings: models, thumbnail layout, the
YouTube category, and per-channel prompt templates and thumbnail template
(`[channels.<name>]` overrides `[defaults]`). Copy `config.toml.example` to
start. The agent can override the three prompt templates at runtime with the
`update_prompts` tool; overrides live in the channel's
`prompt_overrides.json`.

| Variable | Used by | Meaning |
|---|---|---|
| `ASSEMBLYAI_API_KEY` | pipeline | Transcription |
| `OPENROUTER_API_KEY` | pipeline | Clip picking, summaries, thumbnails |
| `CLIPY_MCP_TOKEN` | server, agent | Bearer token for the MCP server, 32+ characters |
| `CLIPY_CHANNEL` | server | Channel to run (default `blow`) |
| `CLIPY_DATA_DIR` | server, CLI | Holds `channels/<channel>/` (default: the repo) |
| `CLIPY_CONFIG` | server, CLI | Path to `config.toml` (default: the repo's) |
| `CLIPY_TIMEZONE` | server | Timezone for notebook timestamps (default `Australia/Sydney`) |
| `YOUTUBE_TOKEN_JSON` | server | Seeds the channel's YouTube token file when it is missing |
| `YOUTUBE_CLIENT_SECRETS` | `clipy auth` | OAuth client file (default `client_secrets.json`) |
| `CODEX_MODEL`, `CODEX_REASONING` | agent | Model and effort (default `gpt-6-sol`, `medium`) |
| `MATRIX_ACCESS_TOKEN` | agent | The report bot's token; no reports without it |
| `MATRIX_HOMESERVER`, `MATRIX_NOTIFY_USER` | agent | Where and to whom reports go |

Secrets for local use go in `.env` (see `.env.example`).

### YouTube access

In the Google Cloud project, enable the YouTube Data API v3, YouTube Analytics
API and YouTube Reporting API, create an OAuth client (Desktop app) saved as
`client_secrets.json`, and keep the OAuth app "In production" (a "Testing"
app's refresh tokens expire after 7 days). Then, signed in as the account that
owns the channel:

```bash
clipy --channel blow auth
```

This saves `channels/<channel>/.youtube_token.json` with upload,
channel-management and Analytics (including revenue) scopes.

## Local use

Install with `uv sync` (or `uv tool install --editable .` for a `clipy`
command). Requires Python 3.14 and ffmpeg.

```bash
clipy --channel blow <youtube-url>   # clip one source into the channel queue
clipy --channel blow --step 5 <url>  # redo from a step (1-6), e.g. thumbnails
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
  prompt.md          The channel policy and daily routine the agent follows
  run.sh             One agent run: codex exec, stats, Matrix report
  events.py          Reads codex's JSON events: live log and the stats line
  notify.py          Posts to the Matrix report room
clipy/
  mcp_server.py      MCP tools the agent uses to run the channel
  jobs.py            Background job runner (clipping, uploads)
  cli.py             Pipeline orchestration and the clipy CLI
  download.py        yt-dlp download and audio extraction
  transcription.py   AssemblyAI transcription with speaker labels
  clips.py           LLM clip picking, summaries and chapters, ffmpeg cutting
  thumbnails.py      Illustration generation and thumbnail compositing
  upload.py          YouTube auth and uploads
  reach.py           Impressions and click-through from reach reports
  queue.py           The channel's clip queue
  costs.py           API spend ledger
  config.py, paths.py, common.py, logging_config.py
tests/               End-to-end tests against the running server
thumbnails/          Channel thumbnail templates
```
