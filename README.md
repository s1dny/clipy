# Blowinator

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

The first run of `upload` will open a browser for OAuth consent. The token is cached at `output/.youtube_token.json` for subsequent runs.

### Optional environment variables

| Variable | Description | Default |
|---|---|---|
| `FFMPEG_PATH` | Path to ffmpeg binary | `/opt/homebrew/bin/ffmpeg` |
| `YT_CLIP_WORK_DIR` | Output directory | `./output` |
| `THUMBNAIL_TEMPLATE` | Path to thumbnail template image | `./thumbnail.jpg` |
| `YOUTUBE_CLIENT_SECRETS` | Path to OAuth client secrets | `./client_secrets.json` |

## Usage

### Full pipeline

```bash
uv run main.py <youtube-url>
```

This runs all 6 steps:

1. Download video and extract audio
2. Transcribe with AssemblyAI
3. Extract clip segments with LLM
4. Cut clips with ffmpeg
5. Generate thumbnails
6. Save to global upload queue

Results are cached per step, so re-running the same URL resumes from where it left off.

### Refresh clips

After editing `clips.json` (e.g. changing titles or thumbnail prompts):

```bash
uv run main.py <youtube-url> refresh
uv run main.py <youtube-url> refresh --force  # regenerate all thumbnails
```

Also regenerates any missing thumbnails from clip folders.

### Upload to YouTube

```bash
uv run main.py upload
```

Uploads pending clips from the global queue (`output/upload_queue.json`) to YouTube as unlisted videos. On first run, assigns a `scheduled_date` to each pending clip in random order, one per day at 00:00 UTC. Only clips whose scheduled date has arrived are uploaded. Run daily (e.g. via cron) to drip-feed one video per day.

### Options

| Flag | Description |
|---|---|
| `--reset` | Clear previous output and start from scratch |
| `--step N` | Force re-run from step N (1-6) |
| `--force` | With `refresh`: regenerate all thumbnails |

## Project structure

```
blow/
  main.py              # Main pipeline
  config.py            # Models, prompts, and settings
  thumbnail.jpg        # Thumbnail template
  client_secrets.json  # YouTube OAuth credentials (not committed)
  output/
    upload_queue.json   # Global upload queue
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
