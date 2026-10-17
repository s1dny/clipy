"""Finding and vetting source videos: YouTube search, a source's details and
captions, and speaker-labelled audio samples."""

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import yt_dlp
from yt_dlp.utils import download_range_func

from .analytics import channel_videos
from .channel import Channel
from .common import run_cmd
from .queue import load_queue
from .transcription import transcribe_audio

logger = logging.getLogger(__name__)

VIDEO_ID_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})")


def processed_sources(channel: Channel) -> set[str]:
    """Source ids to skip: saved clip selections, the queue, the channel's own
    uploads, and the 'Original:' link in each upload's description."""
    seen = (
        {
            p.name
            for p in channel.dir.iterdir()
            if p.is_dir() and (p / "clips.json").exists()
        }
        if channel.dir.exists()
        else set()
    )
    for item in load_queue(channel.queue_file):
        parts = Path(item.get("clip_path", "")).parts
        if "clips" in parts:
            seen.add(parts[parts.index("clips") - 1])
    try:
        for video in channel_videos(channel):
            # Our own uploads show up in searches too; never re-clip a clip.
            seen.add(video["id"])
            seen.update(VIDEO_ID_RE.findall(video["description"]))
    except Exception as exc:  # noqa: BLE001 - dedupe still works from local state
        logger.warning("Could not read channel descriptions: %s", exc)
    return seen


def _ydl(**options: Any) -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **options})


def find_sources(
    channel: Channel,
    query: str | None,
    channel_url: str | None,
    limit: int,
    newest: bool,
) -> list:
    if not query and not channel_url:
        raise ValueError("Pass a search query or a channel/playlist URL.")
    target = channel_url or (
        "https://www.youtube.com/results?"
        + urlencode({"search_query": query} | ({"sp": "CAI="} if newest else {}))
    )
    with _ydl(extract_flat="in_playlist", playlistend=limit) as ydl:
        info = ydl.extract_info(target, download=False)
    done = processed_sources(channel)
    return [
        {
            "url": f"https://www.youtube.com/watch?v={e['id']}",
            "title": e.get("title"),
            "channel": e.get("channel") or e.get("uploader") or info.get("channel"),
            "duration_minutes": round((e.get("duration") or 0) / 60),
            "views": e.get("view_count"),
            "uploaded": e.get("upload_date") or e.get("timestamp"),
            "already_clipped": e["id"] in done,
        }
        for e in (info.get("entries") or [])
        if e and e.get("ie_key") == "Youtube"
    ][:limit]


def _caption_text(ydl: yt_dlp.YoutubeDL, info: dict, limit: int) -> str:
    """Best-effort caption preview. Long streams often have no captions, and
    YouTube rate-limits caption downloads per IP (HTTP 429)."""
    if limit <= 0:
        return ""
    tracks = next(
        (
            group[lang]
            for group in (
                info.get("subtitles") or {},
                info.get("automatic_captions") or {},
            )
            for lang in ("en", "en-orig", "en-US", "en-GB")
            if group.get(lang)
        ),
        None,
    )
    track = next((t for t in tracks or [] if t.get("ext") == "json3"), None)
    if not track:
        return "(no English captions; use sample_source to hear who is talking)"
    try:
        # yt-dlp's own client, so the request impersonates a browser.
        events = json.loads(ydl.urlopen(track["url"]).read()).get("events", [])
    except (yt_dlp.utils.YoutubeDLError, OSError, ValueError) as exc:
        return f"(captions unavailable: {exc}; use sample_source instead)"
    text = " ".join(
        seg.get("utf8", "").strip() for ev in events for seg in ev.get("segs", [])
    )
    return re.sub(r"\s+", " ", text)[:limit]


def sample_source(
    channel: Channel, url: str, start_seconds: float, end_seconds: float
) -> dict:
    ffmpeg = channel.config().ffmpeg
    with _ydl() as ydl:
        info = ydl.extract_info(url, download=False)
    duration = info.get("duration") or 0
    if not 0 <= start_seconds < end_seconds or end_seconds - start_seconds > 900:
        raise ValueError(
            "Choose an interval of up to 15 minutes with 0 <= start < end."
        )
    if duration and end_seconds > duration:
        raise ValueError("The sample extends beyond the source video.")
    ranges = [(start_seconds, end_seconds)]

    with tempfile.TemporaryDirectory() as tmp:
        options = {
            "format": "bestaudio/best",
            "download_ranges": download_range_func(None, ranges),
            "outtmpl": f"{tmp}/part%(section_number)s.%(ext)s",
            # yt-dlp wants a path, and config may name a command on PATH.
            "ffmpeg_location": shutil.which(ffmpeg) or ffmpeg,
        }
        with _ydl(**options) as ydl:
            ydl.download([url])
        parts = sorted(p for p in Path(tmp).iterdir() if p.name.startswith("part"))
        if not parts:
            raise RuntimeError("yt-dlp downloaded no audio samples.")
        sample = Path(tmp) / "sample.mp3"
        inputs = [arg for part in parts for arg in ("-i", str(part))]
        streams = "".join(f"[{i}:a]" for i in range(len(parts)))
        code, _out, err = run_cmd(
            [
                ffmpeg,
                "-y",
                *inputs,
                "-filter_complex",
                f"{streams}concat=n={len(parts)}:v=0:a=1",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(sample),
            ]
        )
        if code != 0:
            raise RuntimeError(f"ffmpeg could not join the samples: {err[-300:]}")
        transcript = transcribe_audio(
            sample,
            os.environ["ASSEMBLYAI_API_KEY"],
            channel.config().models.speech,
        )

    words: dict[str, int] = {}
    lines = []
    for u in transcript.get("utterances") or []:
        words[u["speaker"]] = words.get(u["speaker"], 0) + len(u["text"].split())
        lines.append(f"{u['speaker']}: {u['text']}")
    total = sum(words.values()) or 1
    return {
        "title": info.get("title"),
        "channel": info.get("channel"),
        "sampled_at_minutes": [round(start / 60) for start, _ in ranges],
        "sampled_minutes": round(sum(end - start for start, end in ranges) / 60),
        "speakers": sorted(
            (
                {"speaker": s, "share_of_words_percent": round(100 * n / total)}
                for s, n in words.items()
            ),
            key=lambda row: -row["share_of_words_percent"],
        ),
        "transcript": "\n".join(lines),
    }


def source_info(channel: Channel, url: str, transcript_chars: int) -> dict:
    with _ydl() as ydl:
        info = ydl.extract_info(url, download=False)
        captions = _caption_text(ydl, info, transcript_chars)
    return {
        "url": url,
        "title": info.get("title"),
        "channel": info.get("channel"),
        "uploaded": info.get("upload_date"),
        "duration_minutes": round((info.get("duration") or 0) / 60),
        "views": info.get("view_count"),
        "description": (info.get("description") or "")[:3000],
        "chapters": [
            {"start_minute": round(c["start_time"] / 60), "title": c["title"]}
            for c in info.get("chapters") or []
        ],
        "already_clipped": info["id"] in processed_sources(channel),
        "transcript_preview": captions,
    }
