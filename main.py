import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import assemblyai as aai
import requests
from dotenv import load_dotenv

from PIL import Image

from config import (
    FFMPEG,
    CLIP_MODEL,
    IMAGE_MODEL,
    ASSEMBLYAI_SPEECH_MODELS,
    ILLUSTRATION_SIZE,
    CLIP_EXTRACTION_PROMPT_TEMPLATE,
    ILLUSTRATION_PROMPT_TEMPLATE,
    ILLUSTRATION_CENTER_X,
    ILLUSTRATION_CENTER_Y,
    MAX_TRANSCRIPTION_WAIT_SECONDS,
    HTTP_TIMEOUT,
)

# Constants
YT_DLP = [sys.executable, "-m", "yt_dlp"]
YT_DLP_FORMAT = "bv*+ba/b"
YT_DLP_AUDIO_QUALITY = "0"
THUMBNAIL_JPEG_QUALITY = 2
LLM_SYSTEM_PROMPT = (
    "You are an expert video editor. Return ONLY valid JSON, no markdown."
)


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


def transcribe_audio(audio_path: Path) -> dict:
    """Upload audio to AssemblyAI and get transcript with speaker diarization."""
    print("\n[2/6] Transcribing audio with AssemblyAI...")

    # Use SDK for submission (handles file upload), then poll via REST for reliability
    aai.settings.api_key = ASSEMBLYAI_API_KEY

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
    poll_headers = {"authorization": ASSEMBLYAI_API_KEY}
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


def extract_clips(sentences: list[dict]) -> list[dict]:
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
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": CLIP_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": LLM_SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
            "provider": {"sort": "throughput"},
        },
        timeout=1000,
    )
    if not resp.ok:
        print(f"\tAPI error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"]

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


def generate_illustration(prompt: str, output_path: Path) -> bool:
    """Generate an illustration to be composited onto thumbnail template"""
    print(f"\tGenerating illustration: {output_path.name}...")

    try:
        # Create illustration prompt from config template
        illustration_prompt = ILLUSTRATION_PROMPT_TEMPLATE.format(prompt=prompt)

        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": IMAGE_MODEL,
                "messages": [{"role": "user", "content": illustration_prompt}],
                "provider": {
                    "sort": "throughput",
                },
            },
            timeout=120,
        )
        if not resp.ok:
            print(f"\tAPI error {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()

        data = resp.json()
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


def composite_thumbnail(illustration_path: Path, output_path: Path) -> bool:
    """Crop illustration to non-white content, scale, and center on template."""
    print(f"\tCompositing thumbnail: {output_path.name}...")

    if not THUMBNAIL_TEMPLATE.exists():
        print(f"\tError: Thumbnail template not found at {THUMBNAIL_TEMPLATE}")
        return False

    try:
        template = Image.open(THUMBNAIL_TEMPLATE).convert("RGBA")
        illustration = Image.open(illustration_path).convert("RGBA")

        # Find bounding box of non-white pixels
        # Create a mask where any channel differs significantly from white
        pixels = illustration.load()
        w, h = illustration.size
        threshold = 250
        min_x, min_y, max_x, max_y = w, h, 0, 0
        for y in range(h):
            for x in range(w):
                r, g, b, a = pixels[x, y]
                if a > 10 and (r < threshold or g < threshold or b < threshold):
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)

        if max_x <= min_x or max_y <= min_y:
            print(f"\tNo non-white content found in illustration")
            return False

        # Crop to content
        cropped = illustration.crop((min_x, min_y, max_x + 1, max_y + 1))

        # Scale to fixed size, preserving aspect ratio
        cropped.thumbnail(ILLUSTRATION_SIZE, Image.LANCZOS)

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
            "libx264",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-preset",
            "fast",
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
    """Cut clips from the source video."""
    print("\n[4/6] Cutting clips...")

    clips_dir = work_dir / "clips"
    clips_dir.mkdir(exist_ok=True)

    for i, clip in enumerate(clips):
        folder = clips_dir / clip_folder_name(i, clip["title"])
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


def generate_thumbnails(clips: list[dict], work_dir: Path) -> list[dict]:
    """Generate thumbnails for each clip."""
    print("\n[5/6] Generating thumbnails...")

    clips_dir = work_dir / "clips"

    for i, clip in enumerate(clips):
        folder = clips_dir / clip_folder_name(i, clip["title"])
        folder.mkdir(exist_ok=True)
        thumb_path = folder / "thumbnail.jpg"

        if thumb_path.exists():
            print(f"\tSkipping clip {i + 1}: {clip['title']} (thumbnail exists)")
            clip["thumbnail_path"] = str(thumb_path)
            continue

        illustration_path = folder / "_temp_illustration.png"
        if generate_illustration(
            clip.get("thumbnail_prompt", clip["title"]), illustration_path
        ):
            if composite_thumbnail(illustration_path, thumb_path):
                clip["thumbnail_path"] = str(thumb_path)
            else:
                clip["thumbnail_path"] = None
            if illustration_path.exists():
                illustration_path.unlink()
        else:
            clip["thumbnail_path"] = None

    return clips


def save_queue(clips: list[dict], work_dir: Path):
    """Save clips to upload queue"""
    print("\n[6/6] Saving to upload queue...")

    queue_file = work_dir / "upload_queue.json"

    queue = []
    if queue_file.exists():
        with open(queue_file) as f:
            queue = json.load(f)

    for clip in clips:
        queue.append(
            {**clip, "status": "pending", "created_at": datetime.now().isoformat()}
        )

    with open(queue_file, "w") as f:
        json.dump(queue, f, indent=2)

    print(f"\tQueue saved: {queue_file}")
    print(f"\tTotal clips in queue: {len(queue)}")


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="YouTube Clip Automation")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--reset", action="store_true", help="Reset and start from scratch"
    )
    parser.add_argument(
        "--step", type=int, choices=range(1, 7), metavar="N",
        help="Force restart from step N (1=download, 2=transcribe, 3=extract clips, 4=cut clips, 5=generate thumbnails, 6=save queue)",
    )
    args = parser.parse_args()

    # Env vars (validated early so we fail fast before downloading)
    assemblyai_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not assemblyai_key:
        parser.error("ASSEMBLYAI_API_KEY environment variable not set")
    if not openrouter_key:
        parser.error("OPENROUTER_API_KEY environment variable not set")

    # Make keys available to functions via module globals
    global ASSEMBLYAI_API_KEY, OPENROUTER_API_KEY, WORK_DIR, THUMBNAIL_TEMPLATE
    ASSEMBLYAI_API_KEY = assemblyai_key
    OPENROUTER_API_KEY = openrouter_key

    WORK_DIR = Path(
        os.environ.get("YT_CLIP_WORK_DIR") or Path(__file__).parent / "output"
    )
    THUMBNAIL_TEMPLATE = Path(
        os.environ.get("THUMBNAIL_TEMPLATE") or Path(__file__).parent / "thumbnail.jpg"
    )

    if not THUMBNAIL_TEMPLATE.exists():
        print(f"Warning: Thumbnail template not found at {THUMBNAIL_TEMPLATE}")
        print("Thumbnails will be generated without the template overlay.")

    folder_name = extract_video_id(args.url)
    work_dir = WORK_DIR / folder_name

    print(f"URL: {args.url}")
    print(f"Work dir: {work_dir}")

    # Handle reset
    if args.reset and work_dir.exists():
        shutil.rmtree(work_dir)
        print("Reset: cleared previous output.")

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
            6: ["upload_queue.json"],
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
        print(f"Forcing re-run from step {force_step}.")

    # Check if all steps already completed
    if not force_step and (work_dir / "upload_queue.json").exists():
        print("\nAll steps have already been completed for this URL.")
        resp = input("Would you like to reset and start from scratch? (y/n): ")
        if resp.strip().lower() in ("y", "yes"):
            shutil.rmtree(work_dir)
            print("Reset: cleared previous output.\n")
        else:
            print("Exiting. Use --reset to force a fresh run.")
            return

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
        transcript_file = work_dir / "transcript.json"
        if transcript_file.exists():
            print("\n[2/6] Transcribing audio with AssemblyAI...")
            print("\tSkipping: transcript already exists")
            with open(transcript_file) as f:
                transcript = json.load(f)
        else:
            transcript = transcribe_audio(audio_path)
            with open(transcript_file, "w") as f:
                json.dump(transcript, f, indent=2)

        # Build sentence-level transcript
        sentences_file = work_dir / "sentences.json"
        if sentences_file.exists():
            with open(sentences_file) as f:
                sentences = json.load(f)
        else:
            sentences = build_sentence_transcript(transcript)
            with open(sentences_file, "w") as f:
                json.dump(sentences, f, indent=2)
            print(f"\tSaved {len(sentences)} sentences to {sentences_file.name}")

        # Step 3: Extract clips with LLM
        clips_file = work_dir / "clips.json"
        if clips_file.exists():
            print("\n[3/6] Extracting clip segments with LLM...")
            print("\tSkipping: clips already extracted")
            with open(clips_file) as f:
                clips = json.load(f)
        else:
            clips = extract_clips(sentences)
            with open(clips_file, "w") as f:
                json.dump(clips, f, indent=2)

        # Step 4: Cut clips
        clips = cut_clips(video_path, audio_path, clips, work_dir)

        # Step 5: Generate thumbnails
        clips = generate_thumbnails(clips, work_dir)

        # Step 6: Save queue
        save_queue(clips, work_dir)

        print(f"\nProcessed {len(clips)} clips")
        print(f"Output directory: {work_dir / 'clips'}")

        clips_dir = work_dir / "clips"
        if clips_dir.exists():
            print(f"\nGenerated clips:")
            for folder in sorted(clips_dir.iterdir()):
                if folder.is_dir():
                    print(f"\t{folder.name}/")
                    for item in sorted(folder.iterdir()):
                        if item.is_file():
                            size_mb = item.stat().st_size / (1024 * 1024)
                            print(f"\t  {item.name} ({size_mb:.1f} MB)")

    except Exception as e:
        print(f"\nERROR: {e}")
        traceback.print_exc()
        print(f"\nRe-run with the same URL to resume from the last successful step.")
        sys.exit(1)


if __name__ == "__main__":
    main()
