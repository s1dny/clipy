"""Turning one source video into queued clips: download, transcribe, pick
clips, write their summaries and chapters, cut them, draw thumbnails, queue.

Every step is cached in the source's directory, so an interrupted run
resumes where it stopped.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from . import thumbnails
from .channel import Channel
from .clips import annotate_clip, extract_clips
from .common import load_or_compute, read_json_file, write_json_file
from .config import Config
from .cutting import clips_to_cut, cut_clips
from .download import download_audio, download_video, downloaded
from .queue import save_queue
from .transcription import build_sentence_transcript, transcribe_audio

logger = logging.getLogger(__name__)


def _annotate(clips: list[dict], sentences: list[dict], config: Config) -> None:
    """Give each clip a real summary (its description) and YouTube chapters.
    A failure leaves that clip's short description."""
    todo = [clip for clip in clips if "chapters" not in clip]
    if todo:
        logger.info("[3/6] Writing summaries and chapters for %s clips...", len(todo))

    def annotate(clip: dict) -> None:
        try:
            notes = annotate_clip(sentences, clip, config.models.clip)
        except Exception as exc:  # noqa: BLE001 - chapters are optional
            logger.warning("Could not annotate %r: %s", clip["title"], exc)
            return
        clip["description"] = notes["summary"]
        clip["chapters"] = notes["chapters"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(annotate, todo))


def process_source(channel: Channel, url: str, speaker: str) -> int:
    """Queue clips of `speaker` from the source at `url`; returns how many."""
    config = channel.config()
    source_dir = channel.source_dir(url)
    source_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Processing %s into %s", url, source_dir)

    audio = download_audio(url, source_dir)
    # The video downloads alongside transcription and clip picking, and only
    # if some clip still needs cutting.
    clips_file = source_dir / "clips.json"
    needs_video = not clips_file.exists() or clips_to_cut(
        source_dir, read_json_file(clips_file)
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        video = pool.submit(download_video, url, source_dir) if needs_video else None
        transcript = load_or_compute(
            source_dir / "transcript.json",
            transcribe_audio,
            audio,
            os.environ["ASSEMBLYAI_API_KEY"],
            config.models.speech,
        )
        sentences = load_or_compute(
            source_dir / "sentences.json", build_sentence_transcript, transcript
        )
        clips = load_or_compute(
            clips_file,
            extract_clips,
            sentences,
            config.channel.clip_extraction_prompt_template,
            speaker,
            config.models.clip,
        )
        for clip in clips:  # clips.json from before speakers were recorded
            clip.setdefault("speaker", speaker)
        _annotate(clips, sentences, config)
        write_json_file(clips_file, clips)
        clips = cut_clips(
            video.result if video else None, audio, clips, source_dir, config.ffmpeg
        )
        if video and any(clip["clip_path"] is None for clip in clips):
            # A corrupt or truncated download breaks every clip past the
            # damage; download it again once and cut what failed.
            logger.warning("Some clips failed to cut; downloading the video again")
            if media := downloaded(source_dir, "video"):
                media.unlink()
            clips = cut_clips(
                lambda: download_video(url, source_dir),
                audio,
                clips,
                source_dir,
                config.ffmpeg,
            )
    clips = thumbnails.draw_all(clips, source_dir, config)
    save_queue(clips, channel.queue_file)

    # The clips are cut and the transcript is cached; the source media is only
    # disk usage now (processing the source again downloads it again).
    for stem in ("audio", "video"):
        if media := downloaded(source_dir, stem):
            media.unlink()
    made = sum(1 for clip in clips if clip.get("clip_path"))
    logger.info("Done: %s clips in %s", made, source_dir / "clips")
    return made
