"""Picking clips from a transcript and writing each one's summary and
chapters, with the clip model on OpenRouter."""

import json
import logging

from .common import openrouter_json

logger = logging.getLogger(__name__)
CLIP_TIMEOUT_SECONDS = 3600
ANNOTATE_TIMEOUT_SECONDS = 600


def extract_clips(
    sentences: list[dict], prompt_template: str, speaker: str, model: str
) -> list[dict]:
    """The clips the prompt picks from the transcript, each tagged with the
    source's main speaker. {speaker} in the prompt is that speaker's name."""
    logger.info("[3/6] Picking clips...")
    prompt = prompt_template.replace("{speaker}", speaker)
    clips = openrouter_json(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert video editor. Return only JSON.",
                },
                {
                    "role": "user",
                    "content": f"{prompt}\n\nTranscript:\n{json.dumps(sentences)}",
                },
            ],
            "response_format": {"type": "json_object"},
            "provider": {"sort": "price"},
        },
        CLIP_TIMEOUT_SECONDS,
    )["clips"]
    for i, clip in enumerate(clips):
        clip["speaker"] = speaker
        minutes = (clip["end_ms"] - clip["start_ms"]) / 60000
        logger.info("%s. %s (%.1fm)", i + 1, clip["title"], minutes)
    return clips


ANNOTATE_PROMPT = """Below is the transcript of one YouTube clip of {speaker}, with
timestamps from the start of the clip. Write:

1. "summary": 3-5 sentences in plain language saying what {speaker} argues or
   explains, with the specific ideas, works, or people named. It goes at the
   top of the video description, so make it genuinely useful to someone
   deciding whether to watch; no hype, no claims that aren't made.
2. "chapters": 3-8 chapters marking where the discussion moves to a new point,
   each {{"start_seconds": <int>, "title": "<2-6 words>"}}. The first starts at
   0; chapters are at least 30 seconds apart.

Return only JSON: {{"summary": "...", "chapters": [...]}}"""


def _clock(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def annotate_clip(sentences: list[dict], clip: dict, model: str) -> dict:
    """Summary and chapter list for one clip, from its slice of the transcript."""
    start_ms, end_ms = clip["start_ms"], clip["end_ms"]
    lines = [
        f"[{_clock((s['start'] - start_ms) / 1000)}] {s['text']}"
        for s in sentences
        if start_ms <= s["start"] < end_ms
    ]
    prompt = ANNOTATE_PROMPT.format(speaker=clip["speaker"])
    result = openrouter_json(
        {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt + "\n\n" + "\n".join(lines)}
            ],
            "response_format": {"type": "json_object"},
            "provider": {"sort": "price"},
        },
        ANNOTATE_TIMEOUT_SECONDS,
    )
    return {
        "summary": result["summary"].strip(),
        "chapters": valid_chapters(
            result.get("chapters", []), (end_ms - start_ms) / 1000
        ),
    }


def valid_chapters(chapters: list[dict], duration: float) -> list[dict]:
    """Chapters YouTube will accept: first at 0:00, at least three, each at
    least 10 seconds long. Anything else is dropped rather than half-applied."""
    kept: list[dict] = []
    for chapter in sorted(chapters, key=lambda c: c["start_seconds"]):
        start = int(chapter["start_seconds"])
        if 0 <= start < duration - 10 and (
            not kept or start - kept[-1]["start_seconds"] >= 10
        ):
            kept.append({"start_seconds": start, "title": chapter["title"].strip()})
    if kept:
        kept[0]["start_seconds"] = 0
    return kept if len(kept) >= 3 else []


def chapters_text(chapters: list[dict]) -> str:
    return "\n".join(f"{_clock(c['start_seconds'])} {c['title']}" for c in chapters)
