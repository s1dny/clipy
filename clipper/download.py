import logging
import os
import sys
from pathlib import Path

from .common import run_cmd

FFMPEG = os.environ.get("FFMPEG_PATH", "/opt/homebrew/bin/ffmpeg")

logger = logging.getLogger("clipper")


def download_video(youtube_url: str, work_dir: Path) -> tuple[Path, Path]:
    """Download video from YouTube and extract audio with ffmpeg."""
    logger.info("[1/6] Downloading video and extracting audio...")

    work_dir.mkdir(parents=True, exist_ok=True)

    # Download video with audio
    video_path = work_dir / "video.mp4"
    code, out, err = run_cmd(
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
        logger.error(f"Error downloading video: {err}")
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
        logger.error(f"Error extracting audio: {err}")
        raise Exception(f"ffmpeg audio extraction failed: {err}")

    logger.info(f"Video: {video_path}")
    logger.info(f"Audio: {audio_path}")
    return video_path, audio_path
