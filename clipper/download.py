import logging
import sys
from pathlib import Path

from .common import run_cmd

logger = logging.getLogger(__name__)


class MediaCommandError(RuntimeError):
    """Raised when yt-dlp or FFmpeg fails."""


def download_video(
    youtube_url: str, work_dir: Path, ffmpeg_path: str
) -> tuple[Path, Path]:
    """Download video from YouTube and extract audio with ffmpeg."""
    logger.info("[1/6] Downloading video and extracting audio...")

    work_dir.mkdir(parents=True, exist_ok=True)

    # Download video with audio
    video_path = work_dir / "video.mp4"
    code, _out, err = run_cmd(
        [
            *[sys.executable, "-m", "yt_dlp"],
            "-f",
            "bv*+ba/b",
            "-o",
            str(video_path),
            "--merge-output-format",
            "mp4",
            "--audio-quality",
            "0",
            youtube_url,
        ]
    )
    if code != 0:
        logger.error("Error downloading video: %s", err)
        raise MediaCommandError(f"yt-dlp video failed: {err}")

    # Find the actual video file (yt-dlp may modify filename)
    if not video_path.exists():
        video_files = list(work_dir.glob("video.*"))
        if video_files:
            video_path = video_files[0]
        else:
            raise FileNotFoundError(f"No video file found in {work_dir}")

    # Extract audio with ffmpeg
    audio_path = work_dir / "audio.mp3"
    # Written beside the target and renamed, so a resumed run never mistakes
    # a half-written file for finished audio.
    partial_audio = work_dir / "audio.partial.mp3"
    code, _out, err = run_cmd(
        [
            ffmpeg_path,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-q:a",
            "2",
            str(partial_audio),
        ]
    )
    if code != 0:
        logger.error("Error extracting audio: %s", err)
        raise MediaCommandError(f"ffmpeg audio extraction failed: {err}")
    partial_audio.replace(audio_path)

    logger.info("Video: %s", video_path)
    logger.info("Audio: %s", audio_path)
    return video_path, audio_path
