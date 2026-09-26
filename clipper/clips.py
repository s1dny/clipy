import json
import logging
import re
import sys
from pathlib import Path

from .common import openrouter_message_text, openrouter_request, run_cmd

logger = logging.getLogger(__name__)
CLIP_TIMEOUT_SECONDS = 3600


def extract_clips(
    sentences: list[dict],
    api_key: str,
    prompt_template: str,
    model: str,
) -> list[dict]:
    """Use LLM to extract interesting clip segments from sentence transcript."""
    logger.info("[3/6] Extracting clip segments with LLM...")

    transcript_text = json.dumps(sentences)

    prompt = f"{prompt_template}\n\nTranscript:\n{transcript_text}"

    logger.debug("Using model: %s", model)
    data = openrouter_request(
        api_key,
        {
            "model": model,
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
        CLIP_TIMEOUT_SECONDS,
    )

    content = openrouter_message_text(data)

    try:
        clips = json.loads(content)["clips"]
    except json.JSONDecodeError, KeyError:
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
                logger.debug("Raw content: %s...", content[:500])
                raise

    logger.info("Found %s clips:", len(clips))
    for i, clip in enumerate(clips):
        duration_min = (clip["end_ms"] - clip["start_ms"]) / 60000
        logger.info(
            "%s. %s (%.1fm)",
            i + 1,
            clip["title"],
            duration_min,
            extra={"highlighter": None},
        )

    return clips


def cut_clip(
    video_path: Path,
    start_ms: int,
    end_ms: int,
    output_path: Path,
    ffmpeg_path: str,
) -> bool:
    """Cut and re-encode a clip to high-quality 1080p H.264."""
    start_sec = start_ms / 1000
    duration_sec = (end_ms - start_ms) / 1000

    common = [
        ffmpeg_path,
        "-y",
        "-ss",
        str(start_sec),
        "-t",
        str(duration_sec),
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        "scale=-2:1080:flags=lanczos",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
    ]

    # Fast path for Apple Silicon hardware encoding; VideoToolbox only exists
    # on macOS, so elsewhere go straight to libx264.
    if sys.platform == "darwin":
        code, _out, err = run_cmd(
            [
                *common,
                "-c:v",
                "h264_videotoolbox",
                "-b:v",
                "10M",
                "-maxrate",
                "14M",
                "-bufsize",
                "20M",
                "-profile:v",
                "high",
                "-allow_sw",
                "1",
                "-realtime",
                "0",
                str(output_path),
            ]
        )
        if code == 0:
            return True
        logger.warning("VideoToolbox encode failed, falling back to libx264.")

    code, _out, err = run_cmd(
        [
            *common,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level:v",
            "4.1",
            str(output_path),
        ]
    )

    if code != 0:
        logger.error("FFmpeg error: %s", err)
        return False
    return True


def clip_folder_name(i: int, title: str) -> str:
    """Generate a safe, unique folder name for a clip."""
    safe_name = re.sub(r"[^\w\s-]", "", title).strip() or f"clip_{i + 1}"
    return f"{i + 1:02d} - {safe_name}"


def cut_clips(
    video_path: Path, clips: list[dict], work_dir: Path, ffmpeg_path: str
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
            logger.info("Title changed for clip %s: renaming folder", i + 1)
            existing[0].rename(clips_dir / expected_name)

        folder = clips_dir / expected_name
        folder.mkdir(exist_ok=True)
        clip_path = folder / "video.mp4"

        if clip_path.exists():
            logger.info(
                "Skipping clip %s: %s (already cut)",
                i + 1,
                clip["title"],
                extra={"highlighter": None},
            )
            clip["clip_path"] = str(clip_path)
        else:
            logger.info(
                "Cutting clip %s: %s...",
                i + 1,
                clip["title"],
                extra={"highlighter": None},
            )
            if cut_clip(
                video_path,
                clip["start_ms"],
                clip["end_ms"],
                clip_path,
                ffmpeg_path,
            ):
                clip["clip_path"] = str(clip_path)
            else:
                clip["clip_path"] = None

    return clips
