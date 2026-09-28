"""Source downloads: the audio stream (for transcription) and the video
stream (for cutting) are separate YouTube streams, fetched separately with
yt-dlp's own downloader. Nothing is merged or re-encoded here.

Video is H.264 at up to 1080p when YouTube has it, so clips can be cut by
copying most of the stream instead of re-encoding it (see cutting.py).
"""

import logging
import time
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)

AUDIO_FORMAT = "bestaudio"
ATTEMPTS = 4
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
        # googlevideo stalls now and then on long downloads; retry and resume
        # from the partial file rather than failing the whole job.
        "socket_timeout": 60,
        "retries": 30,
        "fragment_retries": 30,
    }
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
            break
        except yt_dlp.utils.DownloadError as exc:
            if attempt == ATTEMPTS:
                raise
            logger.warning("%s download failed (%s); resuming", stem, exc)
            time.sleep(30 * attempt)
    path = Path(info["requested_downloads"][0]["filepath"])
    logger.info("Downloaded %s: %s", stem, path.name)
    return path


def download_audio(url: str, work_dir: Path) -> Path:
    logger.info("[1/6] Downloading audio...")
    return _download(url, work_dir, "audio", AUDIO_FORMAT)


def download_video(url: str, work_dir: Path) -> Path:
    logger.info("Downloading video (in the background)...")
    return _download(url, work_dir, "video", VIDEO_FORMAT)
