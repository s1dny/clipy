import argparse
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .clips import cut_clips, extract_clips
from .common import load_or_compute
from .config import ChannelConfig, load_channel_config
from .download import download_video
from .logging_config import setup_logging
from .paths import ClipperPaths
from .queue import remove_work_dir_entries, save_queue
from .thumbnails import generate_thumbnails
from .transcription import build_sentence_transcript, transcribe_audio
from .upload import upload_from_queue

logger = logging.getLogger(__name__)

STEP_OUTPUTS: dict[int, list[str]] = {
    1: ["video.mp4", "audio.mp3"],
    2: ["transcript.json", "sentences.json"],
    3: ["clips.json"],
    4: [],
    5: [],
    6: [],
}


@dataclass(frozen=True)
class PipelineRun:
    url: str
    work_dir: Path
    thumbnail_template: Path
    channel_config: ChannelConfig
    assemblyai_key: str
    openrouter_key: str
    force_step: int | None


def _build_parser() -> argparse.ArgumentParser:
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
        help="Thumbnail filename for URL runs; used as the template for generated clip thumbnails.",
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel directory name under ./channels to use as the work root.",
    )
    return parser


def _validate_channel(
    parser: argparse.ArgumentParser, channel: str | None
) -> str | None:
    if channel is None:
        return None
    if "/" in channel or channel in {".", ".."}:
        parser.error("--channel must be a single directory name under ./channels")
    return channel


def _build_paths(channel: str) -> ClipperPaths:
    project_root = Path(__file__).resolve().parent.parent
    work_root = project_root / "channels" / channel
    return ClipperPaths(project_root=project_root, work_root=work_root)


def _require_env(parser: argparse.ArgumentParser, key: str) -> str:
    value = os.environ.get(key, "")
    if not value:
        parser.error(f"{key} environment variable not set")
    return value


def _build_pipeline_run(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    paths: ClipperPaths,
    channel_config: ChannelConfig,
) -> PipelineRun:
    if args.url is None:
        parser.error("URL is required (use 'upload' command to upload from queue)")

    if not args.thumbnail:
        parser.error(
            "--thumbnail is required for URL runs (filename in ./thumbnails or ./thumbnail)"
        )
    if Path(args.thumbnail).name != args.thumbnail:
        parser.error("--thumbnail must be a filename only (no directory path)")

    try:
        thumbnail_template = paths.resolve_upload_thumbnail(args.thumbnail)
    except FileNotFoundError as exc:
        parser.error(str(exc))

    return PipelineRun(
        url=args.url,
        work_dir=paths.work_dir_for_url(args.url),
        thumbnail_template=thumbnail_template,
        channel_config=channel_config,
        assemblyai_key=_require_env(parser, "ASSEMBLYAI_API_KEY"),
        openrouter_key=_require_env(parser, "OPENROUTER_API_KEY"),
        force_step=args.step,
    )


def _force_rerun_from_step(run: PipelineRun, paths: ClipperPaths) -> None:
    """Delete outputs from a step onward so downstream steps are recomputed."""
    for step in range(run.force_step or 0, 7):
        for filename in STEP_OUTPUTS.get(step, []):
            output_path = run.work_dir / filename
            if output_path.exists():
                output_path.unlink()

        clips_dir = run.work_dir / "clips"
        if step == 4 and clips_dir.exists():
            shutil.rmtree(clips_dir)
        elif step == 5 and clips_dir.exists():
            for folder in clips_dir.iterdir():
                if folder.is_dir():
                    for thumbnail in folder.glob("thumbnail.*"):
                        thumbnail.unlink()

        if step == 6 and paths.queue_file.exists():
            remove_work_dir_entries(paths.queue_file, run.work_dir)

    logger.info("Forcing re-run from step %s.", run.force_step)


def _maybe_download(run: PipelineRun) -> tuple[Path, Path]:
    video_path = run.work_dir / "video.mp4"
    audio_path = run.work_dir / "audio.mp3"
    if video_path.exists() and audio_path.exists():
        logger.info("[1/6] Downloading video and extracting audio")
        logger.info("Skipping: video and audio already exist")
        return video_path, audio_path
    return download_video(run.url, run.work_dir)


def _run_pipeline(run: PipelineRun, paths: ClipperPaths) -> None:
    logger.info("URL: %s", run.url)
    logger.info("Work dir: %s", run.work_dir)

    if run.force_step and run.work_dir.exists():
        _force_rerun_from_step(run, paths)

    run.work_dir.mkdir(parents=True, exist_ok=True)

    video_path, audio_path = _maybe_download(run)

    transcript = load_or_compute(
        run.work_dir / "transcript.json",
        transcribe_audio,
        audio_path,
        run.assemblyai_key,
        skip_message="[2/6] Transcribing audio with AssemblyAI... skipped: transcript already exists",
    )
    sentences = load_or_compute(
        run.work_dir / "sentences.json",
        build_sentence_transcript,
        transcript,
    )
    clips = load_or_compute(
        run.work_dir / "clips.json",
        extract_clips,
        sentences,
        run.openrouter_key,
        run.channel_config.clip_extraction_prompt_template,
        skip_message="[3/6] Extracting clip segments with LLM... skipped: clips already extracted",
    )
    clips = cut_clips(video_path, audio_path, clips, run.work_dir)
    clips = generate_thumbnails(
        clips,
        run.work_dir,
        run.openrouter_key,
        run.thumbnail_template,
        run.channel_config.illustration_prompt_template,
    )
    save_queue(clips, paths.queue_file)

    logger.info("Done: %s clips in %s", len(clips), run.work_dir / "clips")


def main() -> None:
    setup_logging()
    load_dotenv()

    parser = _build_parser()
    args = parser.parse_args()
    channel = _validate_channel(parser, args.channel)
    if channel is None:
        parser.error("--channel is required")
    paths = _build_paths(channel)
    try:
        channel_config = load_channel_config(paths.project_root, channel)
    except ValueError as exc:
        parser.error(str(exc))

    if args.url == "upload":
        if args.thumbnail:
            parser.error("--thumbnail is not used with 'upload'.")
        upload_from_queue(
            paths.queue_file,
            channel_config.description_template,
            paths.project_root,
        )
        return

    run = _build_pipeline_run(args, parser, paths, channel_config)
    try:
        _run_pipeline(run, paths)
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        logger.info("Re-run with the same URL to resume from the last successful step.")
        sys.exit(1)
