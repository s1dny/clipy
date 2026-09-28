"""MCP server that lets a scheduled agent run one clip channel end to end.

Everything slow (downloading, transcribing, cutting, uploading) runs as a
background job so a tool call returns in seconds. The tools are mechanism
only: the channel's policy (what to clip, when to publish) lives in the
agent's prompt.
"""

import functools
import hmac
import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import anyio
import requests
import uvicorn
import yt_dlp
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from yt_dlp.utils import download_range_func

from . import matrix, reach
from .cli import build_paths, load_app_config, make_run, run_pipeline
from .common import extract_video_id, read_json_file, run_cmd, write_json_file
from .config import PROMPT_KEYS, AppConfig
from .download import downloaded
from .jobs import JobRunner
from .logging_config import setup_logging
from .queue import item_id, load_queue
from .thumbnails import render_thumbnail
from .transcription import transcribe_audio
from .upload import get_analytics_service, get_youtube_service, upload_video

logger = logging.getLogger(__name__)

CHANNEL = os.environ.get("CLIPY_CHANNEL", "blow")
LOCAL_TZ = ZoneInfo(os.environ.get("CLIPY_TIMEZONE", "Australia/Sydney"))
VIDEO_ID_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})")
DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
CHANNEL_CACHE_SECONDS = 600

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, openWorldHint=False
)

paths = build_paths(CHANNEL)
_channel_cache: tuple[float, list[dict]] | None = None


def _config() -> AppConfig:
    return load_app_config(paths, CHANNEL)


def _token() -> Path:
    return paths.token_file


async def _sync(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def _seconds(iso_duration: str) -> int:
    match = DURATION_RE.fullmatch(iso_duration or "")
    if not match:
        return 0
    d, h, m, s = (int(g or 0) for g in match.groups())
    return ((d * 24 + h) * 60 + m) * 60 + s


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- channel data -----------------------------------------------------------


def _channel_videos(refresh: bool = False) -> list[dict]:
    """Every upload on the channel, including private and scheduled ones."""
    global _channel_cache
    if (
        not refresh
        and _channel_cache
        and time.monotonic() - _channel_cache[0] < CHANNEL_CACHE_SECONDS
    ):
        return _channel_cache[1]

    youtube = get_youtube_service(_token())
    channel = youtube.channels().list(part="contentDetails", mine=True).execute()
    uploads = channel["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    ids: list[str] = []
    page_token = None
    while True:
        page = (
            youtube.playlistItems()
            .list(
                part="contentDetails",
                playlistId=uploads,
                maxResults=50,
                pageToken=page_token,
            )
            .execute()
        )
        ids += [i["contentDetails"]["videoId"] for i in page["items"]]
        page_token = page.get("nextPageToken")
        if not page_token:
            break

    videos = []
    for start in range(0, len(ids), 50):
        batch = (
            youtube.videos()
            .list(
                part="snippet,statistics,contentDetails,status",
                id=",".join(ids[start : start + 50]),
            )
            .execute()
        )
        for v in batch["items"]:
            status = v["status"]
            stats = v.get("statistics", {})
            publish = status.get("publishAt") or v["snippet"]["publishedAt"]
            videos.append(
                {
                    "id": v["id"],
                    "title": v["snippet"]["title"],
                    "description": v["snippet"].get("description", ""),
                    "tags": v["snippet"].get("tags", []),
                    "privacy": status["privacyStatus"],
                    "publish_time": publish,
                    "duration_seconds": _seconds(v["contentDetails"]["duration"]),
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                }
            )
    videos.sort(key=lambda v: v["publish_time"], reverse=True)
    _channel_cache = (time.monotonic(), videos)
    return videos


def _processed_sources() -> set[str]:
    """Source ids to skip: local work dirs, the queue, the channel's own
    uploads, and the 'Original:' link in each upload's description."""
    seen = (
        {p.name for p in paths.work_root.iterdir() if p.is_dir() and len(p.name) == 11}
        if paths.work_root.exists()
        else set()
    )
    for item in load_queue(paths.queue_file):
        parts = Path(item.get("clip_path", "")).parts
        if "clips" in parts:
            seen.add(parts[parts.index("clips") - 1])
    try:
        for video in _channel_videos():
            # Our own uploads show up in searches too; never re-clip a clip.
            seen.add(video["id"])
            seen.update(VIDEO_ID_RE.findall(video["description"]))
    except Exception as exc:  # noqa: BLE001 - dedupe still works from local state
        logger.warning("Could not read channel descriptions: %s", exc)
    return seen


def _reach() -> dict | str:
    """Impressions/CTR history, or why it is unavailable (never fatal)."""
    try:
        return reach.sync(_token(), paths.reach_file)
    except Exception as exc:  # noqa: BLE001 - reports still work without CTR
        logger.warning("Reach reports unavailable: %s", exc)
        return f"unavailable: {exc}"


def _analytics(**query: Any) -> list[dict]:
    response = (
        get_analytics_service(_token())
        .reports()
        .query(ids="channel==MINE", currency="USD", **query)
        .execute()
    )
    names = [h["name"] for h in response.get("columnHeaders", [])]
    return [dict(zip(names, row, strict=True)) for row in response.get("rows", [])]


def _channel_report(days: int) -> dict:
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    window = {"startDate": start.isoformat(), "endDate": end.isoformat()}
    daily = _analytics(
        metrics="views,estimatedMinutesWatched,subscribersGained,estimatedRevenue",
        dimensions="day",
        sort="day",
        **window,
    )
    per_video = _analytics(
        metrics=(
            "views,estimatedMinutesWatched,averageViewDuration,"
            "averageViewPercentage,subscribersGained,estimatedRevenue"
        ),
        dimensions="video",
        sort="-estimatedRevenue",
        maxResults=200,
        **window,
    )
    by_id = {v["id"]: v for v in _channel_videos(refresh=True)}
    reach_data = _reach()
    ctr = reach.totals(reach_data, start, end) if isinstance(reach_data, dict) else {}
    videos = []
    for row in per_video:
        meta = by_id.get(row["video"], {})
        videos.append(
            {
                "id": row["video"],
                "title": meta.get("title"),
                "published": meta.get("publish_time"),
                "duration_minutes": round(meta.get("duration_seconds", 0) / 60, 1),
                "lifetime_views": meta.get("views"),
                "views": row["views"],
                "revenue_usd": round(row["estimatedRevenue"], 2),
                "rpm_usd": round(row["estimatedRevenue"] / row["views"] * 1000, 2)
                if row["views"]
                else 0,
                "avg_view_seconds": row["averageViewDuration"],
                "avg_view_percent": round(row["averageViewPercentage"], 1),
                "subscribers_gained": row["subscribersGained"],
                **ctr.get(row["video"], {}),
            }
        )
    return {
        "window": window,
        "data_through": daily[-1]["day"] if daily else None,
        "note": (
            "YouTube's analytics run about 3 days behind: data_through is the "
            "newest day there is, not yesterday, and impressions/ctr_percent "
            "(daily reach reports) lag about 2 days."
            + ("" if isinstance(reach_data, dict) else f" Reach: {reach_data}")
        ),
        "totals": {
            "views": sum(d["views"] for d in daily),
            "watch_hours": round(sum(d["estimatedMinutesWatched"] for d in daily) / 60),
            "subscribers_gained": sum(d["subscribersGained"] for d in daily),
            "revenue_usd": round(sum(d["estimatedRevenue"] for d in daily), 2),
        },
        "daily": [
            {
                "day": d["day"],
                "views": d["views"],
                "revenue_usd": round(d["estimatedRevenue"], 2),
            }
            for d in daily
        ],
        "videos": videos,
    }


def _video_report(video_id: str, days: int) -> dict:
    end = datetime.now(UTC).date()
    window = {
        "startDate": (end - timedelta(days=days)).isoformat(),
        "endDate": end.isoformat(),
        "filters": f"video=={video_id}",
    }
    retention = _analytics(
        metrics="audienceWatchRatio,relativeRetentionPerformance",
        dimensions="elapsedVideoTimeRatio",
        **window,
    )
    traffic = _analytics(
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
        sort="-views",
        **window,
    )
    report: dict[str, Any] = {
        "video": next((v for v in _channel_videos() if v["id"] == video_id), None),
        "retention": [
            {
                "at_percent": round(r["elapsedVideoTimeRatio"] * 100),
                "still_watching": round(r["audienceWatchRatio"], 3),
                "vs_similar_videos": round(r["relativeRetentionPerformance"], 3),
            }
            for r in retention
            if round(r["elapsedVideoTimeRatio"] * 100) % 10 == 0
        ],
        "traffic_sources": traffic,
    }
    reach_data = _reach()
    report["impressions_daily"] = (
        reach.daily(reach_data, video_id, end - timedelta(days=days))
        if isinstance(reach_data, dict)
        else reach_data
    )
    return report


# --- source discovery -------------------------------------------------------


def _ydl(**options: Any) -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **options})


def _find_sources(
    query: str | None, channel_url: str | None, limit: int, newest: bool
) -> list:
    if not query and not channel_url:
        raise ValueError("Pass a search query or a channel/playlist URL.")
    target = channel_url or (
        "https://www.youtube.com/results?"
        + urlencode({"search_query": query} | ({"sp": "CAI="} if newest else {}))
    )
    with _ydl(extract_flat="in_playlist", playlistend=limit) as ydl:
        info = ydl.extract_info(target, download=False)
    done = _processed_sources()
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


SAMPLE_POINTS = (0.2, 0.5, 0.8)


def _sample_source(url: str, minutes: int) -> dict:
    config = _config()
    with _ydl() as ydl:
        info = ydl.extract_info(url, download=False)
    duration = info.get("duration") or 0
    span = minutes * 60
    if duration <= span * len(SAMPLE_POINTS):
        ranges = [(0, duration or span * len(SAMPLE_POINTS))]
    else:
        ranges = [
            (duration * p - span / 2, duration * p + span / 2) for p in SAMPLE_POINTS
        ]

    with tempfile.TemporaryDirectory() as tmp:
        options = {
            "format": "bestaudio/best",
            "download_ranges": download_range_func(None, ranges),
            "outtmpl": f"{tmp}/part%(section_number)s.%(ext)s",
            # yt-dlp wants a path, and config may name a command on PATH.
            "ffmpeg_location": shutil.which(config.runtime.ffmpeg_path)
            or config.runtime.ffmpeg_path,
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
                config.runtime.ffmpeg_path,
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
            config.models.speech,
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
        "transcript": "\n".join(lines)[:6000],
    }


def _source_info(url: str, transcript_chars: int) -> dict:
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
        "already_clipped": info["id"] in _processed_sources(),
        "transcript_preview": captions,
    }


# --- queue ------------------------------------------------------------------


def _queue() -> list[dict]:
    return load_queue(paths.queue_file)


def _find_item(queue: list[dict], wanted: str) -> dict:
    for item in queue:
        if item_id(item) == wanted:
            return item
    raise ValueError(f"No queued clip with id {wanted!r}; call list_queue.")


def _queue_view(item: dict) -> dict:
    return {
        "id": item_id(item),
        "status": item["status"],
        "title": item["title"],
        "description": item.get("description"),
        "tags": item.get("tags", []),
        "duration_minutes": round(item.get("duration_seconds", 0) / 60, 1) or None,
        "predicted_score": item.get("score"),
        "thumbnail_prompt": item.get("thumbnail_prompt"),
        "chapters": item.get("chapters", []),
        "source_url": "https://www.youtube.com/watch?v=" + item_id(item)[:11],
        "youtube_url": item.get("youtube_url"),
        "publish_at": item.get("publish_at"),
        "discard_reason": item.get("discard_reason"),
    }


def _publish_job(args: dict) -> str:
    config = _config()
    queue = _queue()
    item = _find_item(queue, args["item_id"])
    if item["status"] != "pending":
        raise ValueError(f"Clip is {item['status']}, not pending.")
    item["publish_at"] = args.get("publish_at")
    try:
        upload_video(
            get_youtube_service(_token()),
            item,
            config.channel.description_template,
            config.youtube.category_id,
        )
    except Exception:
        item["status"] = "failed"
        write_json_file(paths.queue_file, queue)
        raise
    item["status"] = "uploaded"
    item["uploaded_at"] = datetime.now(UTC).isoformat()
    write_json_file(paths.queue_file, queue)
    Path(item["clip_path"]).unlink(missing_ok=True)  # YouTube has it now
    return item["youtube_url"]


def _process_job(args: dict) -> str:
    run = make_run(args["url"], paths, _config())
    run_pipeline(run, paths)
    # The clips are cut and the transcript is cached; the source media is only
    # disk usage now (re-running the source downloads it again).
    for stem in ("audio", "video"):
        if media := downloaded(run.work_dir, stem):
            media.unlink()
    source = extract_video_id(args["url"])
    made = [i for i in _queue() if item_id(i).startswith(source)]
    return f"{len(made)} clips from {source} are in the queue."


jobs: JobRunner  # created in main() once the data directory exists


# --- MCP tools --------------------------------------------------------------

mcp = MCPServer(
    name="clipy",
    instructions=(
        f"Runs the '{CHANNEL}' YouTube clip channel: analytics and revenue, finding "
        "source videos, turning them into clips, editing and publishing them, and "
        "editing existing videos. Long work runs as background jobs; follow them "
        "with wait_for_job."
    ),
)


def tool(annotations: ToolAnnotations):
    """Register a tool whose failures reach the agent with their real message."""

    def register(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc

        return mcp.tool(annotations=annotations)(wrapper)

    return register


@tool(READ)
async def channel_report(days: int = 28) -> dict:
    """Channel performance for the last `days` days: totals (views, watch hours,
    subscribers, ad revenue), daily views and revenue up to `data_through` (the
    newest day YouTube has, about 3 days back), and every video with views in
    the window ranked by revenue (with RPM, retention %, lifetime views, and
    thumbnail impressions and click-through rate)."""
    return await _sync(_channel_report, days)


@tool(READ)
async def video_report(video_id: str, days: int = 90) -> dict:
    """Deep dive on one of the channel's videos: metadata, retention curve every
    10% (with performance vs similar videos), traffic sources, and daily
    thumbnail impressions and click-through rate (about 2 days behind)."""
    return await _sync(_video_report, video_id, days)


@tool(READ)
async def list_channel_videos(limit: int = 30) -> list[dict]:
    """The channel's uploads newest first, including scheduled (private) ones,
    with lifetime views/likes/comments and publish time."""
    videos = await _sync(_channel_videos, True)
    return [
        {k: v for k, v in video.items() if k != "description"}
        for video in videos[:limit]
    ]


@tool(READ)
async def find_source_videos(
    query: str | None = None,
    channel_url: str | None = None,
    limit: int = 20,
    newest_first: bool = True,
) -> list[dict]:
    """Find candidate source videos: pass `query` for a YouTube search (newest
    uploads first unless newest_first=false; e.g. "jonathan blow interview"),
    or `channel_url` for a channel's /videos or a playlist (e.g.
    https://www.youtube.com/@jblow888/videos, where his streams are too). Each
    result says whether this channel already clipped it."""
    return await _sync(_find_sources, query, channel_url, limit, newest_first)


@tool(READ)
async def source_video_info(url: str, transcript_chars: int = 6000) -> dict:
    """Details for one candidate source: title, channel, length, description,
    chapters, whether it was already clipped, and the start of its captions
    when YouTube provides them (often it does not: long streams lack captions
    and caption downloads get rate-limited). Free."""
    return await _sync(_source_info, url, transcript_chars)


@tool(READ)
async def sample_source(url: str, minutes_per_sample: int = 3) -> dict:
    """Hear a candidate source before paying to process it: downloads three
    short audio samples (at 20%, 50% and 80% of the video), transcribes them
    with speaker labels, and returns each speaker's share of the words plus the
    labelled transcript, so you can tell whether Jonathan Blow is the main
    speaker and what he talks about. Takes a minute or two."""
    return await _sync(_sample_source, url, max(1, min(minutes_per_sample, 5)))


@tool(WRITE)
async def process_source(url: str) -> dict:
    """Start turning a source video into clips in the background: download,
    transcribe, pick 8-20 minute clips with the clip prompt, cut them, draw
    thumbnails, and add them to the queue as pending. Usually takes a few
    minutes (longer for multi-hour sources) and uses paid transcription and
    LLM calls. Nothing is uploaded. Returns a job; follow it with
    wait_for_job."""
    extract_video_id(url)  # reject anything that is not a video URL
    return jobs.submit("process_source", {"url": url})


@tool(READ)
async def list_jobs(limit: int = 10) -> list[dict]:
    """Recent background jobs (newest first) with status, result, error, and the
    last log lines."""
    return jobs.list(limit)


@tool(READ)
async def wait_for_job(job_id: str, max_minutes: int = 10) -> dict:
    """Wait (server-side, without using tokens) until a job finishes or
    `max_minutes` pass (at most 14), then return it. Use this instead of
    calling list_jobs repeatedly."""
    deadline = time.monotonic() + max(1, min(max_minutes, 14)) * 60
    while True:
        job = next((j for j in jobs.list(10_000) if j["id"] == job_id), None)
        if job is None:
            raise ValueError(f"No job {job_id!r}; see list_jobs.")
        if job["status"] in {"succeeded", "failed"} or time.monotonic() > deadline:
            return job
        await anyio.sleep(10)


@tool(READ)
async def list_queue(status: str = "pending") -> list[dict]:
    """Clips in the local queue with `status` pending, uploaded, failed,
    discarded, or all. Pending clips are ready to publish."""
    items = _queue()
    if status != "all":
        items = [i for i in items if i["status"] == status]
    return [_queue_view(i) for i in items]


@tool(READ)
async def view_thumbnail(clip_id: str | None = None, video_id: str | None = None):
    """Show a thumbnail image: a queued clip's (`clip_id`) or a live channel
    video's (`video_id`)."""
    if clip_id:
        path = Path(_find_item(_queue(), clip_id)["thumbnail_path"])
        return Image(data=path.read_bytes(), format="jpeg")
    if video_id:
        resp = await _sync(
            requests.get,
            f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            timeout=30,
        )
        resp.raise_for_status()
        return Image(data=resp.content, format="jpeg")
    raise ValueError("Pass clip_id or video_id.")


def _edit_clip(
    clip_id: str,
    title: str | None,
    description: str | None,
    tags: list[str] | None,
    thumbnail_prompt: str | None,
) -> dict:
    queue = _queue()
    item = _find_item(queue, clip_id)
    if item["status"] != "pending":
        raise ValueError(f"Clip is {item['status']}; edit the live video instead.")
    if title is not None:
        if len(title) > 100:
            raise ValueError("YouTube titles are at most 100 characters.")
        item["title"] = title
    if description is not None:
        item["description"] = description
    if tags is not None:
        item["tags"] = tags
    if thumbnail_prompt is not None:
        config = _config()
        thumbnail = render_thumbnail(
            thumbnail_prompt,
            Path(item["clip_path"]).parent,
            os.environ["OPENROUTER_API_KEY"],
            paths.thumbnail_template(config.channel.thumbnail_template),
            config.channel,
            config.models.image,
            config.thumbnails,
        )
        if thumbnail is None:
            raise RuntimeError("Thumbnail generation failed; the old one is kept.")
        item["thumbnail_prompt"] = thumbnail_prompt
        item["thumbnail_path"] = str(thumbnail)
    write_json_file(paths.queue_file, queue)
    return _queue_view(item)


@tool(LOCAL_WRITE)
async def edit_clip(
    clip_id: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    thumbnail_prompt: str | None = None,
) -> dict:
    """Change a pending clip before it is published. `description` is the
    summary written during processing; at upload the clip's chapters are
    appended and the channel's description template wraps it with links. A new
    `thumbnail_prompt` (a visual description of the idea, no style words) draws
    a fresh illustration onto the channel's thumbnail template; view it after."""
    return await _sync(_edit_clip, clip_id, title, description, tags, thumbnail_prompt)


@tool(LOCAL_WRITE)
async def discard_clip(clip_id: str, reason: str) -> dict:
    """Drop a pending clip you never want to publish (weak topic, not Jonathan
    Blow talking, duplicate). Nothing on YouTube changes."""
    queue = _queue()
    item = _find_item(queue, clip_id)
    if item["status"] != "pending":
        raise ValueError(f"Clip is {item['status']}, not pending.")
    item["status"] = "discarded"
    item["discard_reason"] = reason
    write_json_file(paths.queue_file, queue)
    Path(item["clip_path"]).unlink(missing_ok=True)
    return _queue_view(item)


@tool(WRITE)
async def publish_clip(clip_id: str, publish_at: str | None = None) -> dict:
    """Upload a pending clip. With `publish_at` (ISO 8601, e.g.
    2026-09-29T14:00:00Z) it goes up private and YouTube publishes it then;
    without it, it goes public as soon as it uploads. Queued uploads run one
    after another. Returns a job; follow it with wait_for_job."""
    item = _find_item(_queue(), clip_id)
    if item["status"] != "pending":
        raise ValueError(f"Clip is {item['status']}, not pending.")
    if any(j["args"]["item_id"] == clip_id for j in jobs.active("publish")):
        raise ValueError(f"{clip_id} is already queued for upload.")
    when = publish_at and _parse_time(publish_at).astimezone(UTC)
    return jobs.submit(
        "publish",
        {
            "item_id": clip_id,
            "publish_at": when and when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )


def _update_video(
    video_id: str,
    title: str | None,
    description: str | None,
    tags: list[str] | None,
) -> dict:
    youtube = get_youtube_service(_token())
    current = youtube.videos().list(part="snippet", id=video_id).execute()["items"]
    if not current:
        raise ValueError(f"No video {video_id} on this channel.")
    snippet = current[0]["snippet"]
    body = {
        "title": title if title is not None else snippet["title"],
        "description": description
        if description is not None
        else snippet.get("description", ""),
        "categoryId": snippet["categoryId"],
        "tags": tags if tags is not None else snippet.get("tags", []),
    }
    if len(body["title"]) > 100:
        raise ValueError("YouTube titles are at most 100 characters.")
    youtube.videos().update(
        part="snippet", body={"id": video_id, "snippet": body}
    ).execute()
    _channel_videos(refresh=True)
    return {"id": video_id, **body}


@tool(WRITE)
async def update_video(
    video_id: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Edit a video already on the channel (retitle an underperformer, fix a
    description). `description` replaces the whole description, so keep the
    'Original:' source link in it. Omitted fields are unchanged."""
    return await _sync(_update_video, video_id, title, description, tags)


def _set_video_thumbnail(video_id: str, thumbnail_prompt: str) -> bytes:
    from googleapiclient.http import MediaFileUpload

    config = _config()
    thumbnail = render_thumbnail(
        thumbnail_prompt,
        paths.work_root / "video-thumbnails" / video_id,
        os.environ["OPENROUTER_API_KEY"],
        paths.thumbnail_template(config.channel.thumbnail_template),
        config.channel,
        config.models.image,
        config.thumbnails,
    )
    if thumbnail is None:
        raise RuntimeError("Thumbnail generation failed; nothing changed.")
    get_youtube_service(_token()).thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg"),
    ).execute()
    return thumbnail.read_bytes()


@tool(WRITE)
async def set_video_thumbnail(video_id: str, thumbnail_prompt: str):
    """Draw a new thumbnail for a live channel video from `thumbnail_prompt` (a
    visual description of the idea) on the channel template, set it on YouTube,
    and return it."""
    data = await _sync(_set_video_thumbnail, video_id, thumbnail_prompt)
    return Image(data=data, format="jpeg")


@tool(READ)
async def get_prompts() -> dict:
    """The prompts the pipeline uses right now: clip_extraction_prompt_template
    (picks clips, titles, descriptions, thumbnail prompts from a transcript),
    illustration_prompt_template (image model; {prompt} is the thumbnail
    prompt), and description_template ({clip_description}, {original_url})."""
    channel = _config().channel
    return {key: getattr(channel, key) for key in PROMPT_KEYS}


@tool(LOCAL_WRITE)
async def update_prompts(
    clip_extraction_prompt_template: str | None = None,
    illustration_prompt_template: str | None = None,
    description_template: str | None = None,
) -> dict:
    """Replace any of the pipeline prompts (see get_prompts). Applies to sources
    processed from now on. The clip prompt must still ask for the JSON shape
    {"clips": [{"title", "description", "start_ms", "end_ms",
    "thumbnail_prompt"}]}; the description template must keep
    {clip_description} and {original_url}."""
    overrides = (
        read_json_file(paths.prompt_overrides_file)
        if paths.prompt_overrides_file.exists()
        else {}
    )
    changes = {
        "clip_extraction_prompt_template": clip_extraction_prompt_template,
        "illustration_prompt_template": illustration_prompt_template,
        "description_template": description_template,
    }
    for key, value in changes.items():
        if value is not None:
            overrides[key] = value
    template = overrides.get("description_template")
    if template and not {"{clip_description}", "{original_url}"} <= set(
        re.findall(r"\{\w+\}", template)
    ):
        raise ValueError(
            "description_template needs {clip_description} and {original_url}."
        )
    if "{prompt}" not in overrides.get("illustration_prompt_template", "{prompt}"):
        raise ValueError("illustration_prompt_template needs {prompt}.")
    write_json_file(paths.prompt_overrides_file, overrides)
    return await get_prompts()


@tool(WRITE)
async def send_report(message: str) -> dict:
    """Send the daily report to the channel owner over Matrix. Markdown
    renders (headings, bold, lists, links). The run's duration and token stats
    are appended automatically when the run ends."""
    if not matrix.configured():
        raise ValueError("Matrix is not configured (MATRIX_ACCESS_TOKEN is unset).")
    return {"event_id": await _sync(matrix.send, message)}


@tool(READ)
async def read_notes() -> str:
    """Your strategy notebook from earlier runs: what was tried, what worked,
    experiments in flight, and the log of past runs. Read it first every run."""
    if not paths.notes_file.exists():
        return "(empty)"
    return paths.notes_file.read_text(encoding="utf-8")


@tool(LOCAL_WRITE)
async def write_notes(content: str, append: bool = True) -> str:
    """Save to the strategy notebook. `append` adds a dated entry at the end;
    append=false replaces the whole notebook (use it to condense old entries
    when it gets long)."""
    paths.notes_file.parent.mkdir(parents=True, exist_ok=True)
    if append:
        stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
        with paths.notes_file.open("a", encoding="utf-8") as file:
            file.write(f"\n\n## {stamp}\n\n{content.strip()}\n")
    else:
        paths.notes_file.write_text(content, encoding="utf-8")
    return f"Saved ({paths.notes_file.stat().st_size} bytes)."


# --- entrypoint -------------------------------------------------------------


async def _healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


class _RequireToken:
    """Every request except /healthz needs `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/healthz":
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                await PlainTextResponse("unauthorized", 401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main() -> None:
    global jobs
    load_dotenv(paths.project_root / ".env")
    setup_logging()
    token = os.environ.get("CLIPY_MCP_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("CLIPY_MCP_TOKEN must be set to at least 32 characters.")

    paths.work_root.mkdir(parents=True, exist_ok=True)
    _config()  # fail fast on a broken config.toml
    jobs = JobRunner(
        paths.jobs_file, {"process_source": _process_job, "publish": _publish_job}
    )

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    app.router.routes.append(Route("/healthz", _healthz))
    uvicorn.run(
        _RequireToken(app, token),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="warning",
    )


__all__ = ["main", "mcp"]


if __name__ == "__main__":
    main()
