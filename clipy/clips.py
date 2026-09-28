import json
import logging
import re
import sys
from pathlib import Path

from .common import openrouter_message_text, openrouter_request, run_cmd

logger = logging.getLogger(__name__)
CLIP_TIMEOUT_SECONDS = 3600


def json_object(text: str) -> dict:
    """The JSON object in a model reply, ignoring any text around it."""
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


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

    data = openrouter_request(
        api_key,
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert video editor. Return only JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "provider": {"sort": "price"},
        },
        CLIP_TIMEOUT_SECONDS,
    )
    clips = json_object(openrouter_message_text(data))["clips"]

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


ANNOTATE_TIMEOUT_SECONDS = 600
ANNOTATE_PROMPT = """Below is the transcript of one YouTube clip of Jonathan Blow, with
timestamps from the start of the clip. Write:

1. "summary": 3-5 sentences in plain language saying what he argues or
   explains, with the specific technologies, games, or people he names. It
   goes at the top of the video description, so make it genuinely useful to
   someone deciding whether to watch; no hype, no claims he doesn't make.
2. "chapters": 3-8 chapters marking where the discussion moves to a new point,
   each {"start_seconds": <int>, "title": "<2-6 words>"}. The first starts at
   0; chapters are at least 30 seconds apart.

Return only JSON: {"summary": "...", "chapters": [...]}"""


def _clock(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def annotate_clip(
    sentences: list[dict], start_ms: int, end_ms: int, api_key: str, model: str
) -> dict:
    """Summary and chapter list for one clip, from its slice of the transcript."""
    lines = [
        f"[{_clock((s['start'] - start_ms) / 1000)}] {s['text']}"
        for s in sentences
        if start_ms <= s["start"] < end_ms
    ]
    data = openrouter_request(
        api_key,
        {
            "model": model,
            "messages": [
                {"role": "user", "content": ANNOTATE_PROMPT + "\n\n" + "\n".join(lines)}
            ],
            "response_format": {"type": "json_object"},
            "provider": {"sort": "price"},
        },
        ANNOTATE_TIMEOUT_SECONDS,
    )
    result = json_object(openrouter_message_text(data))
    return {
        "summary": result["summary"].strip(),
        "chapters": valid_chapters(
            result.get("chapters", []), (end_ms - start_ms) / 1000
        ),
    }


def valid_chapters(chapters: list[dict], duration: float) -> list[dict]:
    """Chapters YouTube will accept: first at 0:00, at least three, each at
    least 10 seconds long. Anything else is dropped rather than half-applied."""
    kept: list[dict] = []
    for chapter in sorted(chapters, key=lambda c: c["start_seconds"]):
        start = int(chapter["start_seconds"])
        if 0 <= start < duration - 10 and (
            not kept or start - kept[-1]["start_seconds"] >= 10
        ):
            kept.append({"start_seconds": start, "title": chapter["title"].strip()})
    if kept:
        kept[0]["start_seconds"] = 0
    return kept if len(kept) >= 3 else []


def chapters_text(chapters: list[dict]) -> str:
    return "\n".join(f"{_clock(c['start_seconds'])} {c['title']}" for c in chapters)


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
    # Encode beside the target and rename on success: a resumed job treats an
    # existing video.mp4 as cut, so it must never be a half-written file.
    partial = output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")

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
                str(partial),
            ]
        )
        if code == 0:
            partial.replace(output_path)
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
            str(partial),
        ]
    )

    if code != 0:
        logger.error("FFmpeg error: %s", err)
        partial.unlink(missing_ok=True)
        return False
    partial.replace(output_path)
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
