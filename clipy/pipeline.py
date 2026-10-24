"""Cache source transcripts, then render and queue the agent's editorial choices."""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from . import thumbnails
from .channel import Channel
from .clips import ClipSelection
from .common import load_or_compute, read_json_file, write_json_file
from .cutting import clips_to_cut, cut_clips
from .download import download_audio, download_video, downloaded
from .queue import save_queue
from .transcription import build_sentence_transcript, transcribe_audio

logger = logging.getLogger(__name__)


def prepare_source(channel: Channel, url: str, include_video: bool = False) -> dict:
    """Prepare the complete timestamped transcript without choosing any clips."""
    source_dir = channel.source_dir(url)
    source_dir.mkdir(parents=True, exist_ok=True)
    transcript_file = source_dir / "transcript.json"
    if not transcript_file.exists():
        transcript = transcribe_audio(
            download_audio(url, source_dir),
            os.environ["ASSEMBLYAI_API_KEY"],
            channel.config().models.speech,
        )
        write_json_file(transcript_file, transcript)
    sentences = load_or_compute(
        source_dir / "sentences.json",
        build_sentence_transcript,
        read_json_file(transcript_file),
    )
    if include_video:
        download_video(url, source_dir)
    return {
        "url": url,
        "sentence_count": len(sentences),
        "video_available": bool(downloaded(source_dir, "video")),
    }


def source_transcript(channel: Channel, url: str, offset: int, limit: int) -> dict:
    """Lossless pages; next_offset is null only when the end has been reached."""
    path = channel.source_dir(url) / "sentences.json"
    if not path.exists():
        raise ValueError("Call prepare_source and let its job finish first.")
    if offset < 0 or not 1 <= limit <= 1000:
        raise ValueError("offset must be nonnegative and limit between 1 and 1000.")
    sentences = read_json_file(path)
    stop = min(offset + limit, len(sentences))
    return {
        "url": url,
        "sentences": sentences[offset:stop],
        "sentence_count": len(sentences),
        "next_offset": stop if stop < len(sentences) else None,
    }


def process_source(
    channel: Channel,
    url: str,
    speaker: str,
    selections: list[ClipSelection] | None = None,
) -> int:
    """Render submitted choices, or resume the saved choices. No editorial API calls."""
    config = channel.config()
    source_dir = channel.source_dir(url)
    source_dir.mkdir(parents=True, exist_ok=True)
    clips_file = source_dir / "clips.json"
    if clips_file.exists():
        clips = read_json_file(clips_file)
        # Media and queue identity are positional. An in-flight or resumed job
        # cannot silently replace the intervals underneath existing exports.
        if selections is not None and [s.model_dump() for s in selections] != [
            ClipSelection.model_validate(c).model_dump() for c in clips
        ]:
            raise ValueError(
                "Selections already saved; resume without clips. Use edit_clip for queued metadata."
            )
    elif selections is None:
        raise ValueError(
            "No saved selections. Read the transcript and submit clips first."
        )
    else:
        sentences_file = source_dir / "sentences.json"
        if not sentences_file.exists():
            raise ValueError(
                "Prepare and read the source transcript before submitting clips."
            )
        sentences = read_json_file(sentences_file)
        duration_ms = max((s["end"] for s in sentences), default=0)
        if any(clip.end_ms > duration_ms for clip in selections):
            raise ValueError("A clip extends beyond the timestamped transcript.")
        clips = [s.model_dump() | {"speaker": speaker} for s in selections]
        write_json_file(clips_file, clips)
    if not clips:
        return 0
    audio = download_audio(url, source_dir)
    needs_video = clips_to_cut(source_dir, clips)
    with ThreadPoolExecutor(max_workers=1) as pool:
        video = pool.submit(download_video, url, source_dir) if needs_video else None
        clips = cut_clips(
            video.result if video else None, audio, clips, source_dir, config.ffmpeg
        )
        if video and any(clip["clip_path"] is None for clip in clips):
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
    # Preserve source media on partial failure so resumption can finish it.
    if all(clip.get("clip_path") and clip.get("thumbnail_path") for clip in clips):
        for stem in ("audio", "video"):
            if media := downloaded(source_dir, stem):
                media.unlink()
    made = sum(1 for clip in clips if clip.get("clip_path"))
    if made != len(clips) or any(not clip.get("thumbnail_path") for clip in clips):
        raise RuntimeError(
            "Some exports or thumbnails failed; resume the saved selections."
        )
    logger.info("Done: %s clips in %s", made, source_dir / "clips")
    return made
