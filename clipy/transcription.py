import logging
import time
from pathlib import Path

import assemblyai as aai
import requests
from rich.progress import SpinnerColumn, TextColumn, TimeElapsedColumn

from . import costs
from .logging_config import create_progress

logger = logging.getLogger(__name__)
MAX_WAIT_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 30


def build_transcription_config(speech_model: str) -> aai.TranscriptionConfig:
    """Build a prerecorded request pinned to one configured speech model."""
    return aai.TranscriptionConfig(
        speech_models=[speech_model],
        speaker_labels=True,
        auto_chapters=False,
    )


def transcribe_audio(
    audio_path: Path, api_key: str, speech_model: str, usd_per_hour: float
) -> dict:
    """Upload audio to AssemblyAI and get transcript with speaker diarization."""
    logger.info("[2/6] Transcribing audio with AssemblyAI...")

    # Use SDK for submission (handles file upload), then poll via REST for reliability
    aai.settings.api_key = api_key

    config = build_transcription_config(speech_model)

    logger.debug("Starting transcription with %s", speech_model)

    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.submit(str(audio_path))

    transcript_id = transcript.id
    if transcript_id is None:
        raise RuntimeError("Transcript ID is None after submission")
    logger.debug("Transcript ID: %s", transcript_id)

    poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    poll_headers = {"authorization": api_key}
    start_time = time.time()

    with create_progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        transient=True,
    ) as progress:
        task_id = progress.add_task("Transcription status: queued", total=None)

        while True:
            elapsed = time.time() - start_time
            if elapsed > MAX_WAIT_SECONDS:
                raise TimeoutError(f"Transcription timed out after {MAX_WAIT_SECONDS}s")

            mins, secs = divmod(int(elapsed), 60)

            try:
                resp = requests.get(
                    poll_url,
                    headers=poll_headers,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                logger.warning("Poll request failed (%s), retrying...", e)
                progress.update(task_id, description="Transcription status: retrying")
                time.sleep(5)
                continue

            status = data["status"]
            progress.update(task_id, description=f"Transcription status: {status}")

            if status == "completed":
                logger.info("Transcription complete (%02d:%02d)", mins, secs)
                audio_hours = (data.get("audio_duration") or 0) / 3600
                costs.record(
                    "assemblyai", audio_hours * usd_per_hour, str(audio_path.parent)
                )
                return data
            if status == "error":
                logger.error("Transcription failed after %02d:%02d", mins, secs)
                raise RuntimeError(
                    f"Transcription failed: {data.get('error', 'Unknown error')}"
                )

            time.sleep(3)


def build_sentence_transcript(transcript: dict) -> list[dict]:
    """Split utterances into per-sentence segments using word-level timings."""
    sentences = []
    for u in transcript.get("utterances", []):
        words = u.get("words", [])
        if not words:
            sentences.append(
                {
                    "speaker": u["speaker"],
                    "text": u["text"],
                    "start": u["start"],
                    "end": u["end"],
                }
            )
            continue
        buf = []
        for w in words:
            buf.append(w)
            if w["text"].rstrip().endswith((".", "?", "!")):
                sentences.append(
                    {
                        "speaker": u["speaker"],
                        "text": " ".join(b["text"] for b in buf),
                        "start": buf[0]["start"],
                        "end": buf[-1]["end"],
                    }
                )
                buf = []
        if buf:
            sentences.append(
                {
                    "speaker": u["speaker"],
                    "text": " ".join(b["text"] for b in buf),
                    "start": buf[0]["start"],
                    "end": buf[-1]["end"],
                }
            )
    return sentences
