import json
import logging
import re
from pathlib import Path

from config import CLIP_EXTRACTION_PROMPT_TEMPLATE, CLIP_EXTRACTION_TIMEOUT, CLIP_MODEL, FFMPEG

from .common import openrouter_request, run_cmd

logger = logging.getLogger("clipper")


def extract_clips(sentences: list[dict], api_key: str) -> list[dict]:
    """Use LLM to extract interesting clip segments from sentence transcript."""
    logger.info("[3/6] Extracting clip segments with LLM...")

    transcript_text = json.dumps(sentences)

    # Build prompt from config template
    prompt = (
        CLIP_EXTRACTION_PROMPT_TEMPLATE
        + f"""

Transcript:
{transcript_text}
"""
    )

    logger.debug(f"Using model: {CLIP_MODEL}")
    data = openrouter_request(
        api_key,
        {
            "model": CLIP_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert video editor. Return ONLY valid JSON, no markdown.",
                },
                {"role": "user", "content": prompt},
            ],
            "provider": {"sort": "price"},
            "thinking": {"type": "enabled", "budget_tokens": 100000},
        },
        CLIP_EXTRACTION_TIMEOUT,
    )

    content = data["choices"][0]["message"]["content"]

    try:
        clips = json.loads(content)["clips"]
    except (json.JSONDecodeError, KeyError):
        logger.warning("JSON parse error, attempting to fix...")
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
                logger.error("Could not parse LLM response")
                logger.debug(f"Raw content: {content[:500]}...")
                raise

    logger.info(f"Found {len(clips)} clips:")
    for i, clip in enumerate(clips):
        duration_min = (clip["end_ms"] - clip["start_ms"]) / 60000
        logger.info(f"{i + 1}. {clip['title']} ({duration_min:.1f}m)")

    return clips


def cut_clip(
    video_path: Path, audio_path: Path, start_ms: int, end_ms: int, output_path: Path
) -> bool:
    """Cut a clip from the video and merge with audio using ffmpeg."""
    start_sec = start_ms / 1000
    duration_sec = (end_ms - start_ms) / 1000

    code, out, err = run_cmd(
        [
            FFMPEG,
            "-y",
            "-hwaccel",
            "videotoolbox",
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
            "-profile:v",
            "high",
            "-allow_sw",
            "1",
            "-realtime",
            "0",
            "-c:a",
            "aac_at",
            "-b:a",
            "192k",
            "-q:a",
            "127",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )

    if code != 0:
        logger.error(f"FFmpeg error: {err}")
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
    logger.info("[4/6] Cutting clips...")

    clips_dir = work_dir / "clips"
    clips_dir.mkdir(exist_ok=True)

    for i, clip in enumerate(clips):
        expected_name = clip_folder_name(i, clip["title"])
        prefix = f"{i + 1:02d} - "

        # Rename folder if title changed
        existing = [
            f for f in clips_dir.iterdir() if f.is_dir() and f.name.startswith(prefix)
        ]
        if existing and existing[0].name != expected_name:
            logger.info(f"Title changed for clip {i + 1}: renaming folder")
            existing[0].rename(clips_dir / expected_name)

        folder = clips_dir / expected_name
        folder.mkdir(exist_ok=True)
        clip_path = folder / "video.mp4"

        if clip_path.exists():
            logger.info(f"Skipping clip {i + 1}: {clip['title']} (already cut)")
            clip["clip_path"] = str(clip_path)
        else:
            logger.info(f"Cutting clip {i + 1}: {clip['title']}...")
            if cut_clip(
                video_path, audio_path, clip["start_ms"], clip["end_ms"], clip_path
            ):
                clip["clip_path"] = str(clip_path)
            else:
                clip["clip_path"] = None

    return clips
