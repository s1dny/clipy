import argparse
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import assemblyai as aai
import requests
from dotenv import load_dotenv

import numpy as np
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

from config import (
    FFMPEG,
    CLIP_MODEL,
    IMAGE_MODEL,
    OPENROUTER_API_URL,
    ASSEMBLYAI_SPEECH_MODELS,
    ILLUSTRATION_SIZE,
    CLIP_EXTRACTION_PROMPT_TEMPLATE,
    ILLUSTRATION_PROMPT_TEMPLATE,
    ILLUSTRATION_CENTER_X,
    ILLUSTRATION_CENTER_Y,
    MAX_TRANSCRIPTION_WAIT_SECONDS,
    HTTP_TIMEOUT,
    CLIP_EXTRACTION_TIMEOUT,
    IMAGE_GENERATION_TIMEOUT,
)

# Constants
YT_DLP = [sys.executable, "-m", "yt_dlp"]
YT_DLP_FORMAT = "bv*+ba/b"
YT_DLP_AUDIO_QUALITY = "0"
LLM_SYSTEM_PROMPT = (
    "You are an expert video editor. Return ONLY valid JSON, no markdown."
)


def openrouter_request(api_key: str, body: dict, timeout: int) -> dict:
    """Make a request to OpenRouter API and return the parsed response."""
    resp = requests.post(
        OPENROUTER_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    if not resp.ok:
        print(f"\tAPI error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json()


def extract_video_id(url: str) -> str:
    """Extract the video ID from a YouTube URL."""
    parsed = urlparse(url)
    if parsed.hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        return parse_qs(parsed.query)["v"][0]
    if parsed.hostname in ("youtu.be",):
        return parsed.path.lstrip("/")
    raise ValueError(f"Cannot extract video ID from URL: {url}")


def run_cmd(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)"""
    print(f"\tRunning: {' '.join(cmd[:5])}...")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return result.returncode, result.stdout, result.stderr


def load_or_compute(path: Path, compute_fn, *args, skip_message: str | None = None):
    """Load JSON from path if it exists, otherwise compute, save, and return."""
    if path.exists():
        if skip_message:
            print(skip_message)
        with open(path) as f:
            return json.load(f)
    result = compute_fn(*args)
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    return result


def download_video(youtube_url: str, work_dir: Path) -> tuple[Path, Path]:
    """Download video from YouTube and extract audio with ffmpeg"""
    print("\n[1/6] Downloading video and extracting audio...")

    work_dir.mkdir(parents=True, exist_ok=True)

    # Download video with audio
    video_path = work_dir / "video.mp4"
    code, out, err = run_cmd(
        [
            *YT_DLP,
            "-f",
            YT_DLP_FORMAT,
            "-o",
            str(video_path),
            "--merge-output-format",
            "mp4",
            "--audio-quality",
            YT_DLP_AUDIO_QUALITY,
            youtube_url,
        ]
    )
    if code != 0:
        print(f"\tError downloading video: {err}")
        raise Exception(f"yt-dlp video failed: {err}")

    # Find the actual video file (yt-dlp may modify filename)
    if not video_path.exists():
        video_files = list(work_dir.glob("video.*"))
        if video_files:
            video_path = video_files[0]
        else:
            raise FileNotFoundError(f"No video file found in {work_dir}")

    # Extract audio with ffmpeg
    audio_path = work_dir / "audio.mp3"
    code, out, err = run_cmd(
        [
            FFMPEG,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-q:a",
            "2",
            str(audio_path),
        ]
    )
    if code != 0:
        print(f"\tError extracting audio: {err}")
        raise Exception(f"ffmpeg audio extraction failed: {err}")

    print(f"\tVideo: {video_path}")
    print(f"\tAudio: {audio_path}")
    return video_path, audio_path


def transcribe_audio(audio_path: Path, api_key: str) -> dict:
    """Upload audio to AssemblyAI and get transcript with speaker diarization."""
    print("\n[2/6] Transcribing audio with AssemblyAI...")

    # Use SDK for submission (handles file upload), then poll via REST for reliability
    aai.settings.api_key = api_key

    config = aai.TranscriptionConfig(
        speech_models=ASSEMBLYAI_SPEECH_MODELS,
        speaker_labels=True,
        auto_chapters=False,
    )

    print(f"\tStarting transcription with {ASSEMBLYAI_SPEECH_MODELS[0]}")

    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.submit(str(audio_path))

    transcript_id = transcript.id
    if transcript_id is None:
        raise RuntimeError("Transcript ID is None after submission")
    print(f"\tTranscript ID: {transcript_id}")

    poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    poll_headers = {"authorization": api_key}
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > MAX_TRANSCRIPTION_WAIT_SECONDS:
            raise TimeoutError(
                f"Transcription timed out after {MAX_TRANSCRIPTION_WAIT_SECONDS}s"
            )

        mins, secs = divmod(int(elapsed), 60)

        try:
            resp = requests.get(poll_url, headers=poll_headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"\n\tWarning: poll request failed ({e}), retrying...")
            time.sleep(5)
            continue

        status = data["status"]
        print(
            f"\r\tStatus: {status} | Elapsed: {mins:02d}:{secs:02d}",
            end="",
            flush=True,
        )

        if status == "completed":
            print(f"\r\tTranscription complete ({mins:02d}:{secs:02d})              ")
            return data
        elif status == "error":
            print(f"\r\tTranscription failed after {mins:02d}:{secs:02d}")
            raise RuntimeError(
                f"Transcription failed: {data.get('error', 'Unknown error')}"
            )

        time.sleep(3)


def build_sentence_transcript(transcript: dict) -> list[dict]:
    """Split utterances into per-sentence segments using word-level timings."""
    sentences = []
    for u in transcript.get("utterances", []):
        words = u.get("words", [])
        if not words:
            sentences.append({"speaker": u["speaker"], "text": u["text"], "start": u["start"], "end": u["end"]})
            continue
        buf = []
        for w in words:
            buf.append(w)
            if w["text"].rstrip().endswith((".", "?", "!")):
                sentences.append({
                    "speaker": u["speaker"],
                    "text": " ".join(b["text"] for b in buf),
                    "start": buf[0]["start"],
                    "end": buf[-1]["end"],
                })
                buf = []
        if buf:
            sentences.append({
                "speaker": u["speaker"],
                "text": " ".join(b["text"] for b in buf),
                "start": buf[0]["start"],
                "end": buf[-1]["end"],
            })
    return sentences


def extract_clips(sentences: list[dict], api_key: str) -> list[dict]:
    """Use LLM to extract interesting clip segments from sentence transcript"""
    print("\n[3/6] Extracting clip segments with LLM...")

    transcript_text = json.dumps(sentences)

    # Build prompt from config template
    prompt = (
        CLIP_EXTRACTION_PROMPT_TEMPLATE
        + f"""

Transcript:
{transcript_text}
"""
    )

    print(f"\tUsing model: {CLIP_MODEL}")
    data = openrouter_request(api_key, {
        "model": CLIP_MODEL,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "provider": {"sort": "price"},
        "thinking": {"type": "enabled", "budget_tokens": 100000},
    }, CLIP_EXTRACTION_TIMEOUT)

    content = data["choices"][0]["message"]["content"]

    try:
        clips = json.loads(content)["clips"]
    except (json.JSONDecodeError, KeyError):
        print(f"\tWarning: JSON parse error, attempting to fix...")
        # Try to extract JSON from markdown code blocks
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if json_match:
            clips = json.loads(json_match.group(1))["clips"]
        else:
            # Try to find JSON object in the response
            json_match = re.search(
                r'\{[^{}]*"clips"\s*:\s*\[.*?\]\s*\}', content, re.DOTALL
            )
            if json_match:
                clips = json.loads(json_match.group(0))["clips"]
            else:
                print(f"\tError: Could not parse LLM response")
                print(f"\tRaw content: {content[:500]}...")
                raise

    print(f"\tFound {len(clips)} clips:")
    for i, clip in enumerate(clips):
        duration_min = (clip["end_ms"] - clip["start_ms"]) / 60000
        print(f"\t  {i + 1}. {clip['title']} ({duration_min:.1f}m)")

    return clips


def generate_illustration(prompt: str, output_path: Path, api_key: str) -> bool:
    """Generate an illustration to be composited onto thumbnail template"""
    print(f"\tGenerating illustration: {output_path.name}...")

    try:
        illustration_prompt = ILLUSTRATION_PROMPT_TEMPLATE.format(prompt=prompt)
        data = openrouter_request(api_key, {
            "model": IMAGE_MODEL,
            "messages": [{"role": "user", "content": illustration_prompt}],
            "provider": {"sort": "price"},
        }, IMAGE_GENERATION_TIMEOUT)

        message = data["choices"][0]["message"]

        # Check for images array (Gemini format)
        images = message.get("images", [])
        if images:
            img_data_url = images[0].get("image_url", {}).get("url", "")
            if img_data_url.startswith("data:image"):
                # Extract base64 data after the comma
                b64_data = img_data_url.split(",", 1)[1]
                img_bytes = base64.b64decode(b64_data)
                with open(output_path, "wb") as f:
                    f.write(img_bytes)
                return True

        print(f"\tNo image in response")
        return False

    except Exception as e:
        print(f"\tIllustration generation failed: {e}")
        return False


def composite_thumbnail(illustration_path: Path, output_path: Path, template_path: Path) -> bool:
    """Crop illustration to non-white content, scale, and center on template."""
    print(f"\tCompositing thumbnail: {output_path.name}...")

    if not template_path.exists():
        print(f"\tError: Thumbnail template not found at {template_path}")
        return False

    try:
        template = Image.open(template_path).convert("RGBA")
        illustration = Image.open(illustration_path).convert("RGBA")

        # Find bounding box of non-white pixels using numpy
        arr = np.array(illustration)
        threshold = 220
        # Mask: alpha > 10 and any RGB channel below threshold
        mask = (arr[:, :, 3] > 10) & (
            (arr[:, :, 0] < threshold) | (arr[:, :, 1] < threshold) | (arr[:, :, 2] < threshold)
        )
        coords = np.argwhere(mask)

        if coords.size == 0:
            print(f"\tNo non-white content found in illustration")
            return False

        # Crop to content (argwhere returns [row, col] = [y, x])
        min_y, min_x = coords.min(axis=0)
        max_y, max_x = coords.max(axis=0)
        cropped = illustration.crop((min_x, min_y, max_x + 1, max_y + 1))

        # Scale to fixed size, preserving aspect ratio
        cropped.thumbnail(ILLUSTRATION_SIZE, Image.Resampling.LANCZOS)

        # Center on the template at the configured coordinates
        paste_x = ILLUSTRATION_CENTER_X - cropped.width // 2
        paste_y = ILLUSTRATION_CENTER_Y - cropped.height // 2
        template.paste(cropped, (paste_x, paste_y), cropped)

        # Save as JPEG
        template.convert("RGB").save(output_path, "JPEG", quality=95)
        return True

    except Exception as e:
        print(f"\tThumbnail compositing failed: {e}")
        return False


def cut_clip(
    video_path: Path, audio_path: Path, start_ms: int, end_ms: int, output_path: Path
) -> bool:
    """Cut a clip from the video and merge with audio using ffmpeg"""
    start_sec = start_ms / 1000
    duration_sec = (end_ms - start_ms) / 1000

    # Use input seeking for faster processing and accurate cutting
    # Map both video from video file and audio from audio file
    code, out, err = run_cmd(
        [
            FFMPEG,
            "-y",
            "-ss",
            str(start_sec),
            "-t",
            str(duration_sec),
            "-i",
            str(video_path),
            "-ss",
            str(start_sec),
            "-t",
            str(duration_sec),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            "8M",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )

    if code != 0:
        print(f"\tFFmpeg error: {err}")
        return False
    return True


def clip_folder_name(i: int, title: str) -> str:
    """Generate a safe, unique folder name for a clip."""
    safe_name = re.sub(r"[^\w\s-]", "", title).strip() or f"clip_{i + 1}"
    return f"{i + 1:02d} - {safe_name}"


def cut_clips(
    video_path: Path, audio_path: Path, clips: list[dict], work_dir: Path
) -> list[dict]:
    """Cut clips from the source video. Detects title changes and renames folders."""
    print("\n[4/6] Cutting clips...")

    clips_dir = work_dir / "clips"
    clips_dir.mkdir(exist_ok=True)

    for i, clip in enumerate(clips):
        expected_name = clip_folder_name(i, clip["title"])
        prefix = f"{i + 1:02d} - "

        # Rename folder if title changed
        existing = [f for f in clips_dir.iterdir() if f.is_dir() and f.name.startswith(prefix)]
        if existing and existing[0].name != expected_name:
            print(f"\tTitle changed for clip {i + 1}: renaming folder")
            existing[0].rename(clips_dir / expected_name)

        folder = clips_dir / expected_name
        folder.mkdir(exist_ok=True)
        clip_path = folder / "video.mp4"

        if clip_path.exists():
            print(f"\tSkipping clip {i + 1}: {clip['title']} (already cut)")
            clip["clip_path"] = str(clip_path)
        else:
            print(f"\tCutting clip {i + 1}: {clip['title']}...")
            if cut_clip(video_path, audio_path, clip["start_ms"], clip["end_ms"], clip_path):
                clip["clip_path"] = str(clip_path)
            else:
                clip["clip_path"] = None

    return clips


def _generate_single_thumbnail(args: tuple) -> tuple[int, str | None]:
    """Generate a single thumbnail (for use with ThreadPoolExecutor)."""
    i, clip, clips_dir, api_key, template_path = args
    folder = clips_dir / clip_folder_name(i, clip["title"])
    folder.mkdir(exist_ok=True)
    thumb_path = folder / "thumbnail.jpg"

    if thumb_path.exists():
        print(f"\tSkipping clip {i + 1}: {clip['title']} (thumbnail exists)")
        return i, str(thumb_path)

    illustration_path = folder / "_temp_illustration.png"
    try:
        if generate_illustration(clip.get("thumbnail_prompt", clip["title"]), illustration_path, api_key):
            if composite_thumbnail(illustration_path, thumb_path, template_path):
                return i, str(thumb_path)
        return i, None
    finally:
        if illustration_path.exists():
            illustration_path.unlink()


def generate_thumbnails(clips: list[dict], work_dir: Path, api_key: str, template_path: Path) -> list[dict]:
    """Generate thumbnails for each clip in parallel. Detects prompt changes and missing thumbnails."""
    print("\n[5/6] Generating thumbnails...")

    clips_dir = work_dir / "clips"

    # Detect prompt changes and delete stale thumbnails so they get regenerated
    for i, clip in enumerate(clips):
        folder = clips_dir / clip_folder_name(i, clip["title"])
        if not folder.exists():
            continue
        prompt_file = folder / ".thumbnail_prompt"
        current_prompt = clip.get("thumbnail_prompt", "").strip()

        if prompt_file.exists():
            if prompt_file.read_text().strip() != current_prompt:
                print(f"\tPrompt changed for clip {i + 1}: {clip['title']}")
                thumb = folder / "thumbnail.jpg"
                if thumb.exists():
                    thumb.unlink()
        prompt_file.write_text(current_prompt)

    tasks = [(i, clip, clips_dir, api_key, template_path) for i, clip in enumerate(clips)]

    with ThreadPoolExecutor(max_workers=10) as pool:
        for i, thumb_path in pool.map(_generate_single_thumbnail, tasks):
            clips[i]["thumbnail_path"] = thumb_path

    return clips


def save_queue(clips: list[dict], work_root: Path):
    """Save clips to the global upload queue in the output directory."""
    print("\n[6/6] Saving to upload queue...")

    queue_file = work_root / "upload_queue.json"

    queue = []
    if queue_file.exists():
        with open(queue_file) as f:
            queue = json.load(f)

    existing_paths = {q["clip_path"] for q in queue}
    for clip in clips:
        if clip.get("clip_path") and clip["clip_path"] not in existing_paths:
            queue.append({
                "title": clip["title"],
                "description": clip["description"],
                "clip_path": clip["clip_path"],
                "thumbnail_path": clip.get("thumbnail_path"),
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

    with open(queue_file, "w") as f:
        json.dump(queue, f, indent=2)

    print(f"\tQueue saved: {queue_file}")
    print(f"\tTotal clips in queue: {len(queue)}")


YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_youtube_service(work_root: Path):
    """Authenticate with YouTube API and return service object."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = work_root / ".youtube_token.json"
    client_secrets = Path(
        os.environ.get("YOUTUBE_CLIENT_SECRETS", Path(__file__).parent / "client_secrets.json")
    )

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            if not client_secrets.exists():
                print(f"Error: YouTube client secrets not found at {client_secrets}")
                print("Download OAuth 2.0 credentials from Google Cloud Console")
                print("and save as client_secrets.json in the project root.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_single_video(youtube, item: dict) -> bool:
    """Upload a single video to YouTube as unlisted."""
    from googleapiclient.http import MediaFileUpload

    title = item["title"]
    video_path = item.get("clip_path")
    thumbnail_path = item.get("thumbnail_path")

    if not video_path or not Path(video_path).exists():
        print(f"  Skipping '{title}': video file not found at {video_path}")
        return False

    print(f"  Uploading: {title}...")

    # Extract original video ID from clip_path: .../output/<video_id>/clips/...
    clip_parts = Path(video_path).parts
    try:
        clips_idx = clip_parts.index("clips")
        source_video_id = clip_parts[clips_idx - 1]
    except (ValueError, IndexError):
        source_video_id = None
    original_url = f"https://youtube.com/watch?v={source_video_id}" if source_video_id else ""

    description = (
        f"Support me on Ko-fi: https://ko-fi.com/jonathanblowclips\n"
        f"{item.get('description', '')}\n"
        f"\n"
        f"Clip from Jonathan Blow\n"
        f"Original: {original_url}\n"
        f"Twitch: https://twitch.tv/j_blow\n"
        f"YouTube: https://www.youtube.com/@jblow888\n"
        f"\n"
        f"#jonathanblow #gamedev #webdevelopment #programming #jai"
    )

    # Schedule for the assigned date at 00:00 UTC if available
    scheduled_date = item.get("scheduled_date")
    if scheduled_date:
        publish_at = f"{scheduled_date}T12:00:00Z"
        status_body = {
            "privacyStatus": "private",
            "publishAt": publish_at,
        }
        print(f"  Scheduled to publish: {scheduled_date}")
    else:
        status_body = {
            "privacyStatus": "unlisted",
        }

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": status_body,
    }

    from tqdm import tqdm

    file_size = Path(video_path).stat().st_size
    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024)

    try:
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        with tqdm(total=file_size, unit="B", unit_scale=True, desc="    Uploading") as pbar:
            while response is None:
                prev = pbar.n
                status, response = request.next_chunk()
                if status:
                    pbar.update(int(status.progress() * file_size) - prev)

        video_id = response["id"]
        item["youtube_id"] = video_id
        item["youtube_url"] = f"https://youtube.com/watch?v={video_id}"
        print(f"  Done: {item['youtube_url']}")

        # Set thumbnail
        if thumbnail_path and Path(thumbnail_path).exists():
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg"),
                ).execute()
                print(f"  Thumbnail set")
            except Exception as e:
                print(f"  Warning: Could not set thumbnail: {e}")

        return True

    except Exception as e:
        print(f"  Upload failed for '{title}': {e}")
        return False


def upload_from_queue(queue_file: Path):
    """Process the upload queue: assign dates and upload due videos."""
    if not queue_file.exists():
        print("No upload queue found. Run the pipeline first.")
        return

    with open(queue_file) as f:
        queue = json.load(f)

    pending = [q for q in queue if q["status"] == "pending"]
    if not pending:
        print("No pending uploads in queue.")
        return

    # Assign scheduled dates to unscheduled items (random order, one per day at 00:00 UTC)
    unscheduled = [q for q in pending if not q.get("scheduled_date")]
    if unscheduled:
        random.shuffle(unscheduled)
        existing_dates = [
            datetime.fromisoformat(q["scheduled_date"]).date()
            for q in queue if q.get("scheduled_date")
        ]
        if existing_dates:
            next_date = max(existing_dates) + timedelta(days=1)
        else:
            next_date = datetime.now(timezone.utc).date() + timedelta(days=1)

        for item in unscheduled:
            item["scheduled_date"] = next_date.isoformat()
            next_date += timedelta(days=1)

        with open(queue_file, "w") as f:
            json.dump(queue, f, indent=2)

    # Upload all pending clips sequentially
    print(f"\n{len(pending)} clip(s) to upload:")
    for item in pending:
        print(f"  {item.get('scheduled_date', 'unscheduled')}: {item['title']}")

    # Authenticate with YouTube
    youtube = get_youtube_service(queue_file.parent)

    for item in pending:
        success = upload_single_video(youtube, item)
        if success:
            item["status"] = "uploaded"
            item["uploaded_at"] = datetime.now(timezone.utc).isoformat()
        else:
            item["status"] = "failed"

        # Save after each upload
        with open(queue_file, "w") as f:
            json.dump(queue, f, indent=2)

    uploaded = sum(1 for item in pending if item["status"] == "uploaded")
    print(f"\nUploaded {uploaded}/{len(pending)} clip(s)")


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="YouTube Clip Automation")
    parser.add_argument("url", nargs="?", default=None, help="YouTube video URL, or 'upload' to upload from queue")
    parser.add_argument(
        "--step", type=int, choices=range(1, 7), metavar="N",
        help="Force restart from step N (1=download, 2=transcribe, 3=extract clips, 4=cut clips, 5=generate thumbnails, 6=save queue)",
    )
    args = parser.parse_args()

    work_root = Path(
        os.environ.get("YT_CLIP_WORK_DIR") or Path(__file__).parent / "output"
    )

    # Handle upload command (no URL needed)
    if args.url == "upload":
        queue_file = work_root / "upload_queue.json"
        upload_from_queue(queue_file)
        return

    if args.url is None:
        parser.error("URL is required (use 'upload' command to upload from queue)")

    # Env vars (validated early so we fail fast before downloading)
    assemblyai_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not openrouter_key:
        parser.error("OPENROUTER_API_KEY environment variable not set")
    if not assemblyai_key:
        parser.error("ASSEMBLYAI_API_KEY environment variable not set")

    thumbnail_template = Path(
        os.environ.get("THUMBNAIL_TEMPLATE") or Path(__file__).parent / "thumbnail.jpg"
    )

    if not thumbnail_template.exists():
        print(f"Warning: Thumbnail template not found at {thumbnail_template}")
        print("Thumbnails will be generated without the template overlay.")

    folder_name = extract_video_id(args.url)
    work_dir = work_root / folder_name

    print(f"URL: {args.url}")
    print(f"Work dir: {work_dir}")

    # Handle --step: delete outputs from that step onward so they re-run
    force_step = args.step
    if force_step and work_dir.exists():
        # Map steps to the files they produce
        step_outputs: dict[int, list[str]] = {
            1: ["video.mp4", "audio.mp3"],
            2: ["transcript.json", "sentences.json"],
            3: ["clips.json"],
            4: [],  # clips/ dir handled separately
            5: [],  # thumbnails inside clips/ handled separately
            6: [],  # global queue handled separately
        }
        for s in range(force_step, 7):
            for f in step_outputs.get(s, []):
                p = work_dir / f
                if p.exists():
                    p.unlink()
            # Steps 4-5: clear the clips directory
            if s in (4, 5):
                clips_dir = work_dir / "clips"
                if s == 4 and clips_dir.exists():
                    shutil.rmtree(clips_dir)
                elif s == 5 and clips_dir.exists():
                    # Only remove thumbnails, keep cut videos
                    for folder in clips_dir.iterdir():
                        if folder.is_dir():
                            for thumb in folder.glob("thumbnail.*"):
                                thumb.unlink()
            # Step 6: remove this video's entries from the global queue
            if s == 6:
                global_queue_file = work_root / "upload_queue.json"
                if global_queue_file.exists():
                    with open(global_queue_file) as f:
                        gq = json.load(f)
                    gq = [q for q in gq if not q.get("clip_path", "").startswith(str(work_dir))]
                    with open(global_queue_file, "w") as f:
                        json.dump(gq, f, indent=2)
        print(f"Forcing re-run from step {force_step}.")

    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: Download video and extract audio
        video_path = work_dir / "video.mp4"
        audio_path = work_dir / "audio.mp3"
        if video_path.exists() and audio_path.exists():
            print("\n[1/6] Downloading video and extracting audio")
            print("\tSkipping: video and audio already exist")
        else:
            video_path, audio_path = download_video(args.url, work_dir)

        # Step 2: Transcribe
        transcript = load_or_compute(
            work_dir / "transcript.json",
            transcribe_audio, audio_path, assemblyai_key,
            skip_message="\n[2/6] Transcribing audio with AssemblyAI...\n\tSkipping: transcript already exists",
        )

        # Build sentence-level transcript
        sentences = load_or_compute(
            work_dir / "sentences.json",
            build_sentence_transcript, transcript,
        )

        # Step 3: Extract clips with LLM
        clips = load_or_compute(
            work_dir / "clips.json",
            extract_clips, sentences, openrouter_key,
            skip_message="\n[3/6] Extracting clip segments with LLM...\n\tSkipping: clips already extracted",
        )

        # Step 4: Cut clips
        clips = cut_clips(video_path, audio_path, clips, work_dir)

        # Step 5: Generate thumbnails
        clips = generate_thumbnails(clips, work_dir, openrouter_key, thumbnail_template)

        # Step 6: Save queue
        save_queue(clips, work_root)

        print(f"\nDone — {len(clips)} clips in {work_dir / 'clips'}")

    except Exception as e:
        print(f"\nERROR: {e}")
        traceback.print_exc()
        print(f"\nRe-run with the same URL to resume from the last successful step.")
        sys.exit(1)


if __name__ == "__main__":
    main()
