import argparse
import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .clips import annotate_clip, cut_clips, extract_clips
from .common import load_or_compute, write_json_file
from .config import AppConfig, load_config
from .download import download_video
from .logging_config import setup_logging
from .paths import ClipyPaths
from .queue import remove_work_dir_entries, save_queue
from .thumbnails import generate_thumbnails
from .transcription import build_sentence_transcript, transcribe_audio
from .upload import authorize

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
    config: AppConfig
    assemblyai_key: str
    openrouter_key: str
    force_step: int | None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="YouTube Clip Automation")
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="YouTube video URL to clip, or 'auth' to authorize YouTube",
    )
    parser.add_argument(
        "--step",
        type=int,
        choices=range(1, 7),
        metavar="N",
        help="Force restart from step N (1=download, 2=transcribe, 3=extract clips, 4=cut clips, 5=generate thumbnails, 6=save queue)",
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel directory name under ./channels to use as the work root.",
    )
    return parser


def _validate_channel(parser: argparse.ArgumentParser, channel: str) -> str:
    if "/" in channel or channel in {".", ".."}:
        parser.error("--channel must be a single directory name under ./channels")
    return channel


def build_paths(channel: str) -> ClipyPaths:
    """Channel state lives under $CLIPY_DATA_DIR/channels, defaulting to the repo."""
    project_root = Path(__file__).resolve().parent.parent
    data_root = Path(os.environ.get("CLIPY_DATA_DIR") or project_root)
    work_root = data_root / "channels" / channel
    return ClipyPaths(project_root=project_root, work_root=work_root)


def load_app_config(paths: ClipyPaths, channel: str) -> AppConfig:
    config_path = Path(
        os.environ.get("CLIPY_CONFIG") or paths.project_root / "config.toml"
    )
    return load_config(config_path, channel, paths.prompt_overrides_file)


def _require_env(parser: argparse.ArgumentParser, key: str) -> str:
    value = os.environ.get(key, "")
    if not value:
        parser.error(f"{key} environment variable not set")
    return value


def make_run(
    url: str, paths: ClipyPaths, config: AppConfig, force_step: int | None = None
) -> PipelineRun:
    """Build a pipeline run; raises KeyError/FileNotFoundError on missing inputs."""
    return PipelineRun(
        url=url,
        work_dir=paths.work_dir_for_url(url),
        thumbnail_template=paths.thumbnail_template(config.channel.thumbnail_template),
        config=config,
        assemblyai_key=os.environ["ASSEMBLYAI_API_KEY"],
        openrouter_key=os.environ["OPENROUTER_API_KEY"],
        force_step=force_step,
    )


def _build_pipeline_run(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    paths: ClipyPaths,
    config: AppConfig,
) -> PipelineRun:
    if args.url is None:
        parser.error("URL is required")
    _require_env(parser, "ASSEMBLYAI_API_KEY")
    _require_env(parser, "OPENROUTER_API_KEY")
    try:
        return make_run(args.url, paths, config, args.step)
    except FileNotFoundError as exc:
        parser.error(str(exc))


def _force_rerun_from_step(run: PipelineRun, paths: ClipyPaths) -> None:
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
                    for generated_thumbnail in folder.glob("generated_thumbnail.*"):
                        generated_thumbnail.unlink()

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
    return download_video(run.url, run.work_dir, run.config.runtime.ffmpeg_path)


def annotate_clips(clips: list[dict], sentences: list[dict], run: PipelineRun):
    """Give each clip a real summary (its description) and YouTube chapters,
    cached in clips.json. A failure leaves that clip's short description."""
    todo = [clip for clip in clips if "chapters" not in clip]
    if not todo:
        return clips
    logger.info("[3/6] Writing summaries and chapters for %s clips...", len(todo))

    def annotate(clip: dict) -> None:
        try:
            notes = annotate_clip(
                sentences,
                clip["start_ms"],
                clip["end_ms"],
                run.openrouter_key,
                run.config.models.clip,
            )
        except Exception as exc:  # noqa: BLE001 - chapters are optional
            logger.warning("Could not annotate %r: %s", clip["title"], exc)
            return
        clip["description"] = notes["summary"]
        clip["chapters"] = notes["chapters"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(annotate, todo))
    write_json_file(run.work_dir / "clips.json", clips)
    return clips


def run_pipeline(run: PipelineRun, paths: ClipyPaths) -> None:
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
        run.config.models.speech,
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
        run.config.channel.clip_extraction_prompt_template,
        run.config.models.clip,
        skip_message="[3/6] Extracting clip segments with LLM... skipped: clips already extracted",
    )
    clips = annotate_clips(clips, sentences, run)
    clips = cut_clips(video_path, clips, run.work_dir, run.config.runtime.ffmpeg_path)
    clips = generate_thumbnails(
        clips,
        run.work_dir,
        run.openrouter_key,
        run.thumbnail_template,
        run.config.channel,
        run.config.models.image,
        run.config.thumbnails,
    )
    save_queue(clips, paths.queue_file)

    logger.info("Done: %s clips in %s", len(clips), run.work_dir / "clips")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    channel = _validate_channel(parser, args.channel)
    paths = build_paths(channel)
    load_dotenv(paths.project_root / ".env")
    try:
        config = load_app_config(paths, channel)
    except ValueError as exc:
        parser.error(str(exc))
    setup_logging()

    if args.url == "auth":
        authorize(paths.token_file, paths.project_root)
        return

    run = _build_pipeline_run(args, parser, paths, config)
    try:
        run_pipeline(run, paths)
    except Exception:
        logger.exception("Pipeline failed")
        logger.info("Re-run with the same URL to resume from the last successful step.")
        sys.exit(1)
