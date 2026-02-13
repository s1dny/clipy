import logging
import sys
import time
from pathlib import Path

import assemblyai as aai
import requests

from config import (
    ASSEMBLYAI_SPEECH_MODELS,
    HTTP_TIMEOUT,
    MAX_TRANSCRIPTION_WAIT_SECONDS,
)

logger = logging.getLogger("clipper")


def transcribe_audio(audio_path: Path, api_key: str) -> dict:
    """Upload audio to AssemblyAI and get transcript with speaker diarization."""
    logger.info("[2/6] Transcribing audio with AssemblyAI...")

    # Use SDK for submission (handles file upload), then poll via REST for reliability
    aai.settings.api_key = api_key

    config = aai.TranscriptionConfig(
        speech_models=ASSEMBLYAI_SPEECH_MODELS,
        speaker_labels=True,
        auto_chapters=False,
    )

    logger.debug(f"Starting transcription with {ASSEMBLYAI_SPEECH_MODELS[0]}")

    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.submit(str(audio_path))

    transcript_id = transcript.id
    if transcript_id is None:
        raise RuntimeError("Transcript ID is None after submission")
    logger.debug(f"Transcript ID: {transcript_id}")

    poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    poll_headers = {"authorization": api_key}
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > MAX_TRANSCRIPTION_WAIT_SECONDS:
            raise TimeoutError(
                f"Transcription timed out after {MAX_TRANSCRIPTION_WAIT_SECONDS}s"
            )

        mins, secs = divmod(int(elapsed), 60)

        try:
            resp = requests.get(poll_url, headers=poll_headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning(f"poll request failed ({e}), retrying...")
            time.sleep(5)
            continue

        status = data["status"]
        # Real-time status line - use print with \r for terminal UX, only if not logging to file
        if sys.stdout.isatty():
            print(
                f"\r\tStatus: {status} | Elapsed: {mins:02d}:{secs:02d}",
                end="",
                flush=True,
            )

        if status == "completed":
            logger.info(f"Transcription complete ({mins:02d}:{secs:02d})")
            return data
        elif status == "error":
            if sys.stdout.isatty():
                print(f"\r\tTranscription failed after {mins:02d}:{secs:02d}")
            logger.error(f"Transcription failed after {mins:02d}:{secs:02d}")
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
