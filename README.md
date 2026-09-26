# Clipper

## Requirements

- Python 3.14+
- [ffmpeg](https://ffmpeg.org/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (installed as a Python dependency)

## Setup

Install project dependencies with:

```bash
uv sync
```

To install a `clipper` command into your CLI with `uv`, run:

```bash
uv tool install --editable .
```

Copy `.env.example` to `.env` and add the two API keys. Secrets are the only
settings kept in `.env`.

Copy `config.toml.example` to `config.toml` and customize it for your channels.

`config.toml` contains all non-secret settings. The `[runtime]`, `[models]`,
`[thumbnails]`, and `[youtube]` tables configure the pipeline. `[models]` groups
the clip, image, and speech model names. `[defaults]` provides shared templates
and `[channels.<name>]` provides channel overrides. Any missing channel value
falls back to `[defaults]`.

### YouTube upload setup

1. Create a project in the [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the **YouTube Data API v3**
3. Create OAuth 2.0 credentials (Desktop app) with the `youtube.upload` scope
4. Download the JSON and save it as `client_secrets.json` in the project root

The first run of `upload` will open a browser for OAuth consent. The token is cached at `<work-dir>/.youtube_token.json` for subsequent runs.

## Development

Format the codebase with Ruff:

```bash
uv run ruff format .
```

Check lint rules with:

```bash
uv run ruff check .
```

## Usage

### Full pipeline

`--channel <name>` is required and writes to `./channels/<name>`.

```bash
clipper --channel philosophy --thumbnail peterson.jpg <youtube-url>
```

This runs all 6 steps:

1. Download video and extract audio
2. Transcribe with AssemblyAI
3. Extract clip segments with LLM
4. Cut clips with ffmpeg
5. Generate thumbnails
6. Save to channel upload queue

Results are cached per step, so re-running the same URL resumes from where it left off. Changes to `clips.json` (titles, thumbnail prompts) and missing thumbnails are detected automatically on re-run.

### Upload to YouTube

For a specific channel directory:

```bash
clipper --channel philosophy upload
```

Uploads all pending clips from the selected channel queue (`channels/<channel>/upload_queue.json`) to YouTube as private, scheduled to publish one per day. Dates are assigned in random order on first run. Each queue item must have a valid `thumbnail_path`.

### Authorize YouTube

```bash
clipper --channel blow auth
```

Opens Google consent in a browser and saves `channels/<channel>/.youtube_token.json`
with upload, channel-management and Analytics (including revenue) scopes. Sign in
as the account that owns the channel, and enable the YouTube Analytics API in the
Google Cloud project. Keep the OAuth app "In production": a "Testing" app's
refresh tokens expire after 7 days.

### MCP server

`clipper-mcp` serves one channel (`CLIPPER_CHANNEL`, default `blow`) over
Streamable HTTP at `/mcp/$CLIPPER_MCP_SECRET` for an agent to run the channel:
analytics and revenue (with API spend from `costs.jsonl`, so profit), source
discovery, clipping sources as background jobs, editing and publishing clips,
editing live videos and thumbnails, prompt overrides, and a strategy notebook.
It refuses a second upload in one day, or a second video going live on one day
(`CLIPPER_TIMEZONE`, default Australia/Sydney).

| Variable | Meaning |
|---|---|
| `CLIPPER_MCP_SECRET` | Required, 32+ characters; the endpoint path is the credential |
| `CLIPPER_DATA_DIR` | Holds `channels/<channel>/` (default: the repo) |
| `CLIPPER_CONFIG` | Path to `config.toml` (default: the repo's) |
| `YOUTUBE_TOKEN_JSON` | Seeds the token file when it is missing |
| `PORT`, `HOST` | Listen address (default `0.0.0.0:8000`) |

Production runs on azalab-0 from `ghcr.io/s1dny/clipper`; see
`flux/clusters/azalab-0/manifests/apps/clipper` in the homelab repo, and
[`docs/daily-agent.md`](docs/daily-agent.md) for the daily agent.

### Options

| Flag | Description |
|---|---|
| `--step N` | Force re-run from step N (1-6), e.g. `--step 5` to regenerate all thumbnails |
| `--channel NAME` | Required. Uses `./channels/NAME` as the work directory |
| `--thumbnail FILE` | Required for URL runs unless the channel sets `thumbnail_template`. Resolved from `./thumbnails/FILE` or `./thumbnail/FILE`, and used as the template for each generated clip thumbnail. |

## Project structure

```bash
clipper/
  main.py              # Thin CLI entrypoint
  pyproject.toml       # Project metadata and dependencies
  config.toml.example  # Minimal config template to copy locally
  config.toml          # Shared defaults + per-channel prompt templates
  uv.lock              # Locked dependency versions
  clipper/
    __init__.py        # Package marker
    cli.py             # Pipeline orchestration and argument parsing
    common.py          # Shared helpers (API calls, command runner, caching)
    download.py        # Video download + audio extraction
    transcription.py   # AssemblyAI transcription + sentence segmentation
    clips.py           # LLM clip extraction + ffmpeg clip cutting
    thumbnails.py      # Illustration + thumbnail generation/compositing
    paths.py           # Centralized path helpers for workspace artifacts
    queue.py           # Upload queue persistence + thumbnail path resolution
    upload.py          # YouTube auth, upload flow and scheduling
    mcp_server.py      # MCP server an agent uses to run a channel
    jobs.py            # Background job runner for the MCP server
    costs.py           # API spend ledger
    logging_config.py  # Logging setup
  client_secrets.json  # YouTube OAuth credentials (not committed)
  tests/               # End-to-end tests against the running MCP server
  thumbnails/
    <template>.jpg     # Thumbnail templates passed via --thumbnail
  channels/
    <channel>/
      upload_queue.json   # Channel upload queue
      .youtube_token.json # OAuth token cache (created after first upload)
      <video-id>/
        video.mp4
        audio.mp3
        transcript.json
        sentences.json
        clips.json
        clips/
          01 - Clip Title/
            video.mp4
            generated_thumbnail.png
            thumbnail.jpg
```
