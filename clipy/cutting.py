"""Cutting clips out of the source video.

Re-encoding a whole 1080p60 clip runs at about 1.6x real time on azalab-0.
A smart cut re-encodes only from the clip's first frame to the next keyframe
(a few seconds) and copies the rest of the H.264 stream untouched, so a clip
takes seconds, starts on the exact frame, and is mostly the original video
rather than a re-encode of it. Sources that aren't H.264 are re-encoded in
full. Audio comes from the separate audio stream, encoded to AAC.
"""

import logging
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from .common import run_cmd

logger = logging.getLogger(__name__)

KEYFRAME_SEARCH_SECONDS = 30
MAX_SHORTFALL_SECONDS = 1.0
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "192k"]


def _ffprobe(ffmpeg_path: str) -> str:
    return str(Path(ffmpeg_path).with_name("ffprobe"))


def _video_codec(ffprobe: str, video: Path) -> str:
    _code, out, _err = run_cmd(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name", "-of", "csv=p=0", str(video)]
    )  # fmt: skip
    return out.strip().split(",")[0]


def _next_keyframe(ffprobe: str, video: Path, at: float) -> float | None:
    """The first keyframe at or after `at` seconds."""
    _code, out, _err = run_cmd(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-read_intervals", f"{at}%+{KEYFRAME_SEARCH_SECONDS}",
         "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(video)]
    )  # fmt: skip
    for line in out.splitlines():
        value = line.split(",")[0].strip()
        if value and float(value) >= at - 0.001:
            return float(value)
    return None


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


def _smart_video(ffmpeg, ffprobe, video, start, end, tmp: Path) -> Path | None:
    """The clip's video as a copied stream with a re-encoded head, or None
    when there is no keyframe to copy from."""
    key = _next_keyframe(ffprobe, video, start)
    if key is None or key >= end:
        return None
    parts = []
    if key - start > 0.001:
        head = tmp / "head.ts"
        _run([ffmpeg, "-v", "error", "-y", "-ss", str(start), "-i", str(video),
              "-t", str(key - start), "-an", *X264, "-f", "mpegts", str(head)])  # fmt: skip
        parts.append(head)
    tail = tmp / "tail.ts"
    _run([ffmpeg, "-v", "error", "-y", "-ss", str(key), "-i", str(video),
          "-t", str(end - key), "-an", "-c:v", "copy",
          "-bsf:v", "h264_mp4toannexb", "-f", "mpegts", str(tail)])  # fmt: skip
    parts.append(tail)
    playlist = tmp / "parts.txt"
    playlist.write_text("".join(f"file '{part}'\n" for part in parts))
    return playlist


def cut_clip(
    video: Path,
    audio: Path,
    start_ms: int,
    end_ms: int,
    output: Path,
    ffmpeg_path: str,
    *,
    smart: bool = True,
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
        with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
            playlist = None
            if smart and _video_codec(ffprobe, video) == "h264":
                playlist = _smart_video(
                    ffmpeg_path, ffprobe, video, start, end, Path(tmp)
                )
            if playlist:
                _run([ffmpeg_path, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                      "-i", str(playlist), *audio_input, "-c:v", "copy", *finish])  # fmt: skip
            else:
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
    return clips


def clips_to_cut(work_dir: Path, clips: list[dict]) -> bool:
    """Whether any clip still needs cutting (and so needs the source video)."""
    return any(
        not (
            work_dir / "clips" / clip_folder_name(i, c["title"]) / "video.mp4"
        ).exists()
        for i, c in enumerate(clips)
    )
