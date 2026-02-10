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

### Optional environment variables

| Variable | Description | Default |
|---|---|---|
| `FFMPEG_PATH` | Path to ffmpeg binary | `/opt/homebrew/bin/ffmpeg` |
| `YT_CLIP_WORK_DIR` | Output directory | `./output` |
| `THUMBNAIL_TEMPLATE` | Path to thumbnail template image | `./thumbnail.jpg` |

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
6. Save upload queue

Results are cached per step, so re-running the same URL resumes from where it left off.

### Refresh clips

After editing `clips.json` (e.g. changing titles or thumbnail prompts):

```bash
uv run main.py <youtube-url> refresh
uv run main.py <youtube-url> refresh --force  # regenerate all thumbnails
```

### Options

| Flag | Description |
|---|---|
| `--reset` | Clear previous output and start from scratch |
| `--step N` | Force re-run from step N (1-6) |
| `--force` | With `refresh`: regenerate all thumbnails |

## Project structure

```
blow/
  main.py            # Main pipeline
  config.py          # Models, prompts, and settings
  thumbnail.jpg      # Thumbnail template
  output/             # Generated output (per video ID)
    <video-id>/
      video.mp4
      audio.mp3
      transcript.json
      sentences.json
      clips.json
      upload_queue.json
      clips/
        01 - Clip Title/
          video.mp4
          thumbnail.jpg
```
