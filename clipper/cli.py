import argparse
import json
import logging
import os
import shutil
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

from .clips import cut_clips, extract_clips
from .common import extract_video_id, load_or_compute
from .download import download_video
from .logging_config import setup_logging
from .queue import resolve_upload_thumbnail_path, save_queue
from .thumbnails import generate_thumbnails
from .transcription import build_sentence_transcript, transcribe_audio
from .upload import upload_from_queue

logger = logging.getLogger("clipper")


def _force_rerun_from_step(work_dir: Path, work_root: Path, force_step: int):
    """Delete outputs from a step onward so downstream steps are recomputed."""
    # Map steps to the files they produce
    step_outputs: dict[int, list[str]] = {
        1: ["video.mp4", "audio.mp3"],
        2: ["transcript.json", "sentences.json"],
        3: ["clips.json"],
        4: [],  # clips/ dir handled separately
        5: [],  # thumbnails inside clips/ handled separately
        6: [],  # global queue handled separately
    }
    for s in range(force_step, 7):
        for f in step_outputs.get(s, []):
            p = work_dir / f
            if p.exists():
                p.unlink()
        # Steps 4-5: clear the clips directory
        if s in (4, 5):
            clips_dir = work_dir / "clips"
            if s == 4 and clips_dir.exists():
                shutil.rmtree(clips_dir)
            elif s == 5 and clips_dir.exists():
                # Only remove thumbnails, keep cut videos
                for folder in clips_dir.iterdir():
                    if folder.is_dir():
                        for thumb in folder.glob("thumbnail.*"):
                            thumb.unlink()
        # Step 6: remove this video's entries from the global queue
        if s == 6:
            global_queue_file = work_root / "upload_queue.json"
            if global_queue_file.exists():
                with open(global_queue_file) as f:
                    gq = json.load(f)
                gq = [
                    q for q in gq if not q.get("clip_path", "").startswith(str(work_dir))
                ]
                with open(global_queue_file, "w") as f:
                    json.dump(gq, f, indent=2)
    logger.info(f"Forcing re-run from step {force_step}.")


def main():
    setup_logging()
    load_dotenv()

    parser = argparse.ArgumentParser(description="YouTube Clip Automation")
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="YouTube video URL, or 'upload' to upload from queue",
    )
    parser.add_argument(
        "--step",
        type=int,
        choices=range(1, 7),
        metavar="N",
        help="Force restart from step N (1=download, 2=transcribe, 3=extract clips, 4=cut clips, 5=generate thumbnails, 6=save queue)",
    )
    parser.add_argument(
        "--thumbnail",
        help="Thumbnail filename for URL runs; saved per clip in queue as upload_thumbnail.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    work_root = Path(os.environ.get("YT_CLIP_WORK_DIR") or project_root / "output")

    # Handle upload command (no URL needed)
    if args.url == "upload":
        if args.thumbnail:
            parser.error(
                "--thumbnail is not used with 'upload'. "
                "Set it when running a URL so each queue item gets upload_thumbnail metadata."
            )

        queue_file = work_root / "upload_queue.json"
        upload_from_queue(queue_file, project_root)
        return

    if args.url is None:
        parser.error("URL is required (use 'upload' command to upload from queue)")

    queue_upload_thumbnail: str | None = None
    if args.thumbnail:
        if Path(args.thumbnail).name != args.thumbnail:
            parser.error("--thumbnail must be a filename only (no directory path)")
        try:
            resolve_upload_thumbnail_path(args.thumbnail)
        except FileNotFoundError as e:
            parser.error(str(e))
        queue_upload_thumbnail = args.thumbnail

    # Env vars (validated early so we fail fast before downloading)
    assemblyai_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not openrouter_key:
        parser.error("OPENROUTER_API_KEY environment variable not set")
    if not assemblyai_key:
        parser.error("ASSEMBLYAI_API_KEY environment variable not set")

    thumbnail_template = Path(
        os.environ.get("THUMBNAIL_TEMPLATE") or project_root / "thumbnail.jpg"
    )

    if not thumbnail_template.exists():
        logger.warning(f"Thumbnail template not found at {thumbnail_template}")
        logger.info("Thumbnails will be generated without the template overlay.")

    folder_name = extract_video_id(args.url)
    work_dir = work_root / folder_name

    logger.info(f"URL: {args.url}")
    logger.info(f"Work dir: {work_dir}")

    # Handle --step: delete outputs from that step onward so they re-run
    force_step = args.step
    if force_step and work_dir.exists():
        _force_rerun_from_step(work_dir, work_root, force_step)

    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: Download video and extract audio
        video_path = work_dir / "video.mp4"
        audio_path = work_dir / "audio.mp3"
        if video_path.exists() and audio_path.exists():
            logger.info("[1/6] Downloading video and extracting audio")
            logger.info("Skipping: video and audio already exist")
        else:
            video_path, audio_path = download_video(args.url, work_dir)

        # Step 2: Transcribe
        transcript = load_or_compute(
            work_dir / "transcript.json",
            transcribe_audio,
            audio_path,
            assemblyai_key,
            skip_message="\n[2/6] Transcribing audio with AssemblyAI...\n\tSkipping: transcript already exists",
        )

        # Build sentence-level transcript
        sentences = load_or_compute(
            work_dir / "sentences.json",
            build_sentence_transcript,
            transcript,
        )

        # Step 3: Extract clips with LLM
        clips = load_or_compute(
            work_dir / "clips.json",
            extract_clips,
            sentences,
            openrouter_key,
            skip_message="\n[3/6] Extracting clip segments with LLM...\n\tSkipping: clips already extracted",
        )

        # Step 4: Cut clips
        clips = cut_clips(video_path, audio_path, clips, work_dir)

        # Step 5: Generate thumbnails
        clips = generate_thumbnails(clips, work_dir, openrouter_key, thumbnail_template)

        # Step 6: Save queue
        save_queue(clips, work_root, queue_upload_thumbnail)

        logger.info(f"Done — {len(clips)} clips in {work_dir / 'clips'}")

    except Exception as e:
        logger.error(f"ERROR: {e}")
        traceback.print_exc()
        logger.info("Re-run with the same URL to resume from the last successful step.")
        sys.exit(1)
