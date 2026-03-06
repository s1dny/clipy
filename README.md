# Clipper

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (installed as a Python dependency)

## Setup

```bash
uv sync
```

Create a `.env` file with your API keys:

```
ASSEMBLYAI_API_KEY=your_key
OPENROUTER_API_KEY=your_key
```

### YouTube upload setup

1. Create a project in the [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the **YouTube Data API v3**
3. Create OAuth 2.0 credentials (Desktop app) with the `youtube.upload` scope
4. Download the JSON and save it as `client_secrets.json` in the project root

The first run of `upload` will open a browser for OAuth consent. The token is cached at `<work-dir>/.youtube_token.json` for subsequent runs.

### Optional environment variables

| Variable | Description | Default |
|---|---|---|
| `FFMPEG_PATH` | Path to ffmpeg binary | `/opt/homebrew/bin/ffmpeg` |
| `YT_CLIP_WORK_DIR` | Work directory | `./output` (used by direct `uv run main.py`) |
| `YOUTUBE_CLIENT_SECRETS` | Path to OAuth client secrets | `./client_secrets.json` |

## Usage

### Full pipeline

`clipper.sh` requires `--channel <name>` and writes to `./channels/<name>`.

```bash
./clipper.sh --channel philosophy --thumbnail peterson.jpg <youtube-url>
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
./clipper.sh --channel philosophy upload
```

Uploads all pending clips from the selected channel queue (`channels/<channel>/upload_queue.json`) to YouTube as private, scheduled to publish one per day. Dates are assigned in random order on first run. Each queue item must have `upload_thumbnail` metadata.

Mixed-host workflow (same channel/work directory):

1. Run each source video with its own `--thumbnail` (for example `peterson.jpg`, `harris.jpg`).
2. Upload with `./clipper.sh --channel philosophy upload`.

### Options

| Flag | Description |
|---|---|
| `--step N` | Force re-run from step N (1-6), e.g. `--step 5` to regenerate all thumbnails |
| `--channel NAME` | Required. Uses `./channels/NAME` as the work directory |
| `--thumbnail FILE` | Required for URL runs. Resolved from `./thumbnails/FILE` or `./thumbnail/FILE`, used as the thumbnail template for generation, and saved per clip in queue as `upload_thumbnail` metadata. |

## Project structure

```
clipper/
  main.py              # Thin CLI entrypoint
  clipper/
    cli.py             # Pipeline orchestration and argument parsing
    common.py          # Shared helpers (API calls, command runner, caching)
    download.py        # Video download + audio extraction
    transcription.py   # AssemblyAI transcription + sentence segmentation
    clips.py           # LLM clip extraction + ffmpeg clip cutting
    thumbnails.py      # Illustration + thumbnail generation/compositing
    queue.py           # Upload queue persistence + thumbnail path resolution
    upload.py          # YouTube upload flow and scheduling
    logging_config.py  # Logging setup
  config.py            # Models, prompts, and settings
  client_secrets.json  # YouTube OAuth credentials (not committed)
  channels/
    <channel>/
      upload_queue.json   # Channel upload queue
      .youtube_token.json
      <video-id>/
        video.mp4
        audio.mp3
        transcript.json
        sentences.json
        clips.json
        clips/
          01 - Clip Title/
            video.mp4
            thumbnail.jpg
```
