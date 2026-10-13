"""Frame-accurate clips with both streams encoded from the same source range."""

import logging
import re
from collections.abc import Callable
from pathlib import Path

from .common import run_cmd

logger = logging.getLogger(__name__)

MAX_SHORTFALL_SECONDS = 1.0
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "192k"]


def _ffprobe(ffmpeg_path: str) -> str:
    return str(Path(ffmpeg_path).with_name("ffprobe"))


def _duration(ffprobe: str, media: Path) -> float:
    _code, out, _err = run_cmd(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(media)]
    )  # fmt: skip
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def _run(cmd: list[str]) -> None:
    code, _out, err = run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"ffmpeg failed: {err[-500:]}")


def cut_clip(
    video: Path,
    audio: Path,
    start_ms: int,
    end_ms: int,
    output: Path,
    ffmpeg_path: str,
) -> bool:
    """Cut [start_ms, end_ms) into an MP4 at output; False on failure."""
    start, end = start_ms / 1000, end_ms / 1000
    ffprobe = _ffprobe(ffmpeg_path)
    # Written beside the target and renamed on success: a resumed job treats
    # an existing video.mp4 as cut, so it must never be half-written.
    partial = output.with_name(f"{output.stem}.partial{output.suffix}")
    audio_input = ["-ss", str(start), "-t", str(end - start), "-i", str(audio)]
    finish = ["-map", "0:v:0", "-map", "1:a:0", *AAC, "-shortest",
              "-movflags", "+faststart", str(partial)]  # fmt: skip
    try:
        # Copying H.264 after a re-encoded head can repeat frames around the
        # splice while audio advances normally. Encode the entire range so
        # video and audio retain one continuous timeline.
        _run([ffmpeg_path, "-v", "error", "-y", "-ss", str(start),
              "-t", str(end - start), "-i", str(video), *audio_input,
              *X264, *finish])  # fmt: skip
        # -shortest ends the clip early when the source video does (a
        # truncated download), so a short clip means a broken source.
        length = _duration(ffprobe, partial)
        if length < end - start - MAX_SHORTFALL_SECONDS:
            raise RuntimeError(
                f"clip is {length:.1f}s, not {end - start:.1f}s; the source is cut short"
            )
    except RuntimeError as exc:
        logger.error("Cutting failed: %s", exc)
        partial.unlink(missing_ok=True)
        return False
    partial.replace(output)
    output.with_suffix(".encoding").write_text("full")
    return True


def clip_folder_name(i: int, title: str) -> str:
    """Generate a safe, unique folder name for a clip."""
    safe_name = re.sub(r"[^\w\s-]", "", title).strip() or f"clip_{i + 1}"
    return f"{i + 1:02d} - {safe_name}"


def cut_clips(
    video: Callable[[], Path] | None,
    audio: Path,
    clips: list[dict],
    work_dir: Path,
    ffmpeg_path: str,
) -> list[dict]:
    """Cut every clip not cut yet; renames a clip's folder if its title changed.
    `video` returns the source video (waiting for its download) and is only
    called when a clip needs cutting."""
    logger.info("[4/6] Cutting clips...")
    clips_dir = work_dir / "clips"
    clips_dir.mkdir(exist_ok=True)

    for i, clip in enumerate(clips):
        expected_name = clip_folder_name(i, clip["title"])
        prefix = f"{i + 1:02d} - "
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
            logger.info("Skipping clip %s: %s (already cut)", i + 1, clip["title"])
        else:
            logger.info("Cutting clip %s: %s...", i + 1, clip["title"])
            if video is None:
                raise RuntimeError("Clips need cutting but no video was downloaded.")
            if not cut_clip(
                video(), audio, clip["start_ms"], clip["end_ms"], clip_path, ffmpeg_path
            ):
                clip["clip_path"] = None
                continue
        clip["clip_path"] = str(clip_path)
        marker = clip_path.with_suffix(".encoding")
        clip["cut_encoding"] = marker.read_text() if marker.exists() else "legacy"
    return clips


def clips_to_cut(work_dir: Path, clips: list[dict]) -> bool:
    """Whether any clip still needs cutting (and so needs the source video)."""
    return any(
        not (
            work_dir / "clips" / clip_folder_name(i, c["title"]) / "video.mp4"
        ).exists()
        for i, c in enumerate(clips)
    )
