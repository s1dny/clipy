"""Source downloads: the audio stream (for transcription) and the video
stream (for cutting) are separate YouTube streams, fetched separately with
yt-dlp's own downloader. Nothing is merged or re-encoded here.

Video is H.264 at up to 1080p when YouTube has it, so clips can be cut by
copying most of the stream instead of re-encoding it (see cutting.py).
"""

import logging
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)

AUDIO_FORMAT = "bestaudio"
VIDEO_FORMAT = "bv*[vcodec^=avc1][height<=1080]/bv*[height<=1080]/bv*"


def downloaded(work_dir: Path, stem: str) -> Path | None:
    """The finished audio.* or video.* file in work_dir, if there is one.
    yt-dlp renames a download into place only once it is complete."""
    return next(
        (
            path
            for path in sorted(work_dir.glob(f"{stem}.*"))
            if path.suffix not in {".part", ".ytdl"} and ".part" not in path.suffixes
        ),
        None,
    )


def _download(url: str, work_dir: Path, stem: str, fmt: str) -> Path:
    existing = downloaded(work_dir, stem)
    if existing:
        logger.info("Skipping %s download: %s already exists", stem, existing.name)
        return existing
    work_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "format": fmt,
        "outtmpl": str(work_dir / f"{stem}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
    path = Path(info["requested_downloads"][0]["filepath"])
    logger.info("Downloaded %s: %s", stem, path.name)
    return path


def download_audio(url: str, work_dir: Path) -> Path:
    logger.info("[1/6] Downloading audio...")
    return _download(url, work_dir, "audio", AUDIO_FORMAT)


def download_video(url: str, work_dir: Path) -> Path:
    logger.info("Downloading video (in the background)...")
    return _download(url, work_dir, "video", VIDEO_FORMAT)
