"""MCP server that lets a scheduled agent run clip channels end to end.

Each channel in config.toml is served at /mcp/<channel>; a tool call acts on
the channel in its URL. Everything slow (downloading, transcribing, cutting,
uploading) runs as a background job so a tool call returns in seconds. The
tools are mechanism only: each channel's policy (who to clip, when to
publish) lives in the agent's prompt.
"""

import contextvars
import functools
import hmac
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anyio
import requests
import uvicorn
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import (
    analytics,
    headshots,
    performance,
    recovery,
    sources,
    stats,
    studio,
    thumbnails,
)
from .channel import PROJECT_ROOT, Channel, all_channels, data_root
from .common import extract_video_id, read_json_file, write_json_file
from .config import PROMPT_KEYS
from .jobs import JobRunner
from .logging_config import setup_logging
from .pipeline import process_source as run_pipeline
from .queue import item_id, load_queue
from .upload import set_thumbnail, upload_video

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo(os.environ.get("CLIPY_TIMEZONE", "Australia/Sydney"))

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, openWorldHint=False
)

_channel: contextvars.ContextVar[Channel] = contextvars.ContextVar("channel")


def ch() -> Channel:
    """The channel this request is for (from its /mcp/<channel> URL)."""
    return _channel.get()


async def _sync(fn, *args, **kwargs):
    # to_thread copies the context, so ch() works in the worker thread too.
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


# --- queue and jobs ---------------------------------------------------------


def _queue() -> list[dict]:
    return load_queue(ch().queue_file)


def _find_item(queue: list[dict], wanted: str) -> dict:
    for item in queue:
        if item_id(item) == wanted:
            return item
    raise ValueError(f"No queued clip with id {wanted!r}; call list_queue.")


def _pending(queue: list[dict], clip_id: str) -> dict:
    item = _find_item(queue, clip_id)
    if item["status"] != "pending":
        raise ValueError(f"Clip is {item['status']}, not pending.")
    return item


def _queue_view(item: dict) -> dict:
    return {
        "id": item_id(item),
        "status": item["status"],
        "title": item["title"],
        "speaker": item.get("speaker"),
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
        "thumbnail_pending": item.get("thumbnail_pending", False),
        "processing_status": item.get("processing_status"),
        "repair": item.get("repair"),
    }


def _publish_job(name: str, args: dict) -> str:
    channel = Channel(name)
    queue = load_queue(channel.queue_file)
    item = _find_item(queue, args["item_id"])
    if item["status"] == "failed" or item.get("youtube_id"):
        # A lost response can follow a successful upload. Reconcile with
        # YouTube before starting another resumable session.
        source = item_id(item).split(":")[0]
        matches = [
            video
            for video in analytics.channel_videos(channel, refresh=True)
            if (
                video["id"] == item.get("youtube_id")
                or (video["title"] == item["title"] and source in video["description"])
            )
        ]
        if len(matches) > 1:
            raise ValueError(
                "Multiple YouTube uploads match this failed clip; inspect them before retrying."
            )
        if matches:
            if matches[0].get("upload_status") not in {"uploaded", "processed"}:
                raise ValueError(
                    "A matching YouTube upload is not complete; check its status before retrying."
                )
            item["follow_comment_pending"] = True
            item["youtube_id"] = matches[0]["id"]
            item["youtube_url"] = f"https://youtube.com/watch?v={matches[0]['id']}"
            set_thumbnail(channel.youtube(), item)
            item["status"] = "uploaded"
            item["uploaded_at"] = datetime.now(UTC).isoformat()
            write_json_file(channel.queue_file, queue)
            if matches[0].get("upload_status") == "processed":
                Path(item["clip_path"]).unlink(missing_ok=True)
            return item["youtube_url"]
    elif item["status"] != "pending":
        raise ValueError(f"Clip is {item['status']}, not pending or failed.")
    item["publish_at"] = args.get("publish_at")
    config = channel.config().channel
    try:
        if item.get("cut_encoding") != "full":
            # Pending clips cut by the old splice path must also be rebuilt;
            # otherwise cached media could carry the same drift into new uploads.
            recovery._rebuild(channel, item)
        upload_video(
            channel.youtube(),
            item,
            config.description_template,
            config.category_id,
            on_uploaded=lambda uploaded: recovery.save_item(channel, uploaded),
        )
    except Exception:
        item["status"] = "failed"
        write_json_file(channel.queue_file, queue)
        raise
    item["status"] = "uploaded"
    item["uploaded_at"] = datetime.now(UTC).isoformat()
    write_json_file(channel.queue_file, queue)
    # Keep the MP4 until a later processing check confirms it is playable.
    return item["youtube_url"]


def _thumbnails_job(name: str, _args: dict) -> str:
    """Retry the thumbnails YouTube refused at upload (usually its per-account
    rate limit), stopping at the first refusal."""
    channel = Channel(name)
    queue = load_queue(channel.queue_file)
    pending = [i for i in queue if i.get("thumbnail_pending") and i.get("youtube_id")]
    youtube = channel.youtube()
    done = 0
    for item in pending:
        if not set_thumbnail(youtube, item):
            break
        done += 1
    write_json_file(channel.queue_file, queue)
    return f"Set {done} of {len(pending)} pending thumbnails."


def _process_job(name: str, args: dict) -> str:
    made = run_pipeline(Channel(name), args["url"], args["speaker"])
    return f"{made} clips of {args['speaker']} are in the queue."


jobs: JobRunner  # created in main() once the data directory exists


# --- MCP tools --------------------------------------------------------------

mcp = MCPServer(
    name="clipy",
    instructions=(
        "Runs one YouTube clip channel (the one in this server's URL): analytics, "
        "finding source videos, turning them into clips, editing and publishing "
        "them, and editing existing videos. Long work runs as background jobs; "
        "follow them with wait_for_job."
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
async def channel_report(
    days: int = 28, rank_by: str = "revenue", limit: int = 20
) -> dict:
    """Channel performance for the last `days` days: totals (views, watch hours,
    subscribers, ad revenue), daily views, watch hours and revenue up to
    `data_through` (the newest day YouTube has, about 3 days back), and every
    video with views in the window with its watch hours, revenue, RPM,
    retention %, lifetime views, and thumbnail impressions and click-through
    rate. Videos are ranked by `rank_by`: "revenue" or "watch_hours"."""
    return await _sync(analytics.channel_report, ch(), days, rank_by, limit)


@tool(READ)
async def recent_performance(limit: int = 20) -> dict:
    """Current video counters and measured 24/48-hour changes from hourly
    snapshots, plus channel subscribers. Faster provisional signals than
    Analytics; no invented realtime CTR, revenue or watch hours."""
    result = await _sync(performance.recent, ch())
    result["tracked_video_count"] = len(result["videos"])
    result["videos"] = result["videos"][: max(1, min(limit, 200))]
    result["studio"] = studio.latest(ch())
    return result


@tool(READ)
async def video_report(video_id: str, days: int = 90) -> dict:
    """Deep dive on one of the channel's videos: metadata, retention curve every
    10% (with performance vs similar videos), traffic sources, and daily
    thumbnail impressions and click-through rate (about 2 days behind)."""
    return await _sync(analytics.video_report, ch(), video_id, days)


@tool(READ)
async def list_channel_videos(limit: int = 30) -> list[dict]:
    """The channel's uploads newest first, including scheduled (private) ones,
    with lifetime views/likes/comments and publish time."""
    videos = await _sync(analytics.channel_videos, ch(), True)
    by_id = {v["id"]: v for v in videos}
    queue = _queue()
    changed = False
    for item in queue:
        if video := by_id.get(item.get("youtube_id")):
            for key, value in {
                "publish_at": video["publish_time"],
                "processing_status": video["processing_status"],
            }.items():
                if item.get(key) != value:
                    item[key] = value
                    changed = True
            if video["upload_status"] == "processed" and video["duration_seconds"] > 0:
                Path(item["clip_path"]).unlink(missing_ok=True)
    if changed:
        write_json_file(ch().queue_file, queue)
    return [
        {k: v for k, v in video.items() if k != "description"}
        for video in videos[:limit]
    ]


@tool(READ)
async def inspect_upload(video_id: str) -> dict:
    """Get YouTube's actual processing status and failure reason for a Clipy
    upload. Zero duration or a thumbnail 404 alone do not prove failure."""
    if not any(
        i.get("youtube_id") == video_id
        or i.get("repair", {}).get("original_id") == video_id
        for i in _queue()
    ):
        raise ValueError("Video is not linked to this channel's Clipy queue.")
    return await _sync(recovery.inspect, ch().youtube(), video_id)


@tool(WRITE)
async def repair_upload(video_id: str, publish_at: str | None = None) -> dict:
    """Repair a private, unwatched Clipy upload with failed processing or
    processing stalled for at least 24 hours. Rebuilds and fully re-encodes
    the original excerpt, uploads privately, verifies processing, transfers
    its future schedule, then deletes the broken copy. Never retries a policy
    rejection or replaces a working/public video. Allowed any day. Call again
    after a processing timeout to resume the same replacement. Returns a job."""
    if active := jobs.active(ch().name, "repair"):
        return active[0]
    if jobs.active(ch().name, "publish"):
        raise ValueError(
            "Wait for this channel's active publish jobs before repairing."
        )
    if not any(
        i.get("youtube_id") == video_id
        or i.get("repair", {}).get("original_id") == video_id
        for i in _queue()
    ):
        raise ValueError("Video is not linked to this channel's Clipy queue.")
    return jobs.submit(
        ch().name, "repair", {"video_id": video_id, "publish_at": publish_at}
    )


@tool(READ)
async def find_source_videos(
    query: str | None = None,
    channel_url: str | None = None,
    limit: int = 20,
    newest_first: bool = True,
) -> list[dict]:
    """Find candidate source videos: pass `query` for a YouTube search (newest
    uploads first unless newest_first=false; e.g. "robert sapolsky lecture"),
    or `channel_url` for a channel's /videos or a playlist. Each result says
    whether this channel already clipped it."""
    return await _sync(
        sources.find_sources, ch(), query, channel_url, limit, newest_first
    )


@tool(READ)
async def source_video_info(url: str, transcript_chars: int = 6000) -> dict:
    """Details for one candidate source: title, channel, length, description,
    chapters, whether it was already clipped, and the start of its captions
    when YouTube provides them (often it does not: long streams lack captions
    and caption downloads get rate-limited). Free."""
    return await _sync(sources.source_info, ch(), url, transcript_chars)


@tool(READ)
async def sample_source(url: str, minutes_per_sample: int = 3) -> dict:
    """Hear a candidate source before paying to process it: downloads three
    short audio samples (at 20%, 50% and 80% of the video), transcribes them
    with speaker labels, and returns each speaker's share of the words plus the
    labelled transcript, so you can tell who the main speaker is and what they
    talk about. Takes a minute or two."""
    minutes = max(1, min(minutes_per_sample, 5))
    return await _sync(sources.sample_source, ch(), url, minutes)


@tool(WRITE)
async def process_source(url: str, speaker: str | None = None) -> dict:
    """Start turning a source video into clips of `speaker`, its main speaker
    (full name; a channel about one person has a default), in the background:
    download, transcribe, pick 8-20 minute clips with the clip prompt, write
    summaries and chapters, cut them, draw thumbnails, and add them to the
    queue as pending. Channels without a fixed thumbnail template need the
    speaker's headshot saved first. Usually takes a few minutes and
    uses paid transcription and LLM calls. Nothing is uploaded. Returns a job;
    follow it with wait_for_job."""
    extract_video_id(url)  # reject anything that is not a video URL
    config = ch().config().channel
    speaker = speaker or config.speaker
    if not speaker:
        raise ValueError("Name the source's main speaker.")
    if not config.thumbnail_template:
        headshots.panel(speaker)  # says where to save it if it is missing
    return jobs.submit(ch().name, "process_source", {"url": url, "speaker": speaker})


@tool(WRITE)
async def retry_thumbnails() -> dict:
    """Set the thumbnails YouTube refused when their clips were uploaded
    (list_queue shows them as thumbnail_pending; YouTube rate-limits
    thumbnail uploads per account). Runs as a background job; there is no
    need to wait for it."""
    if not any(i.get("thumbnail_pending") for i in _queue()):
        raise ValueError("No uploaded clip is waiting for its thumbnail.")
    if active := jobs.active(ch().name, "thumbnails"):
        return active[0]
    return jobs.submit(ch().name, "thumbnails", {})


@tool(READ)
async def list_jobs(limit: int = 10) -> list[dict]:
    """This channel's recent background jobs (newest first) with status,
    result, error, and the last log lines."""
    return jobs.list(ch().name, limit)


@tool(READ)
async def wait_for_job(job_id: str, max_minutes: int = 10) -> dict:
    """Wait (server-side, without using tokens) until a job finishes or
    `max_minutes` pass (at most 14), then return it. Use this instead of
    calling list_jobs repeatedly."""
    deadline = time.monotonic() + max(1, min(max_minutes, 14)) * 60
    while True:
        job = next((j for j in jobs.list(ch().name, 10_000) if j["id"] == job_id), None)
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
        path = _find_item(_queue(), clip_id).get("thumbnail_path")
        if not path:
            raise ValueError("That clip has no thumbnail; draw one with edit_clip.")
        return Image(data=Path(path).read_bytes(), format="jpeg")
    if video_id:
        current = await _sync(
            lambda: ch().youtube().videos().list(part="snippet", id=video_id).execute()
        )
        if not current.get("items"):
            raise ValueError(f"No video {video_id}.")
        choices = current["items"][0]["snippet"].get("thumbnails", {})
        for size in ("maxres", "standard", "high", "medium", "default"):
            if size not in choices:
                continue
            resp = await _sync(requests.get, choices[size]["url"], timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            return Image(data=resp.content, format="jpeg")
        raise ValueError(
            "YouTube has no available thumbnail rendition; inspect_upload for processing status, or view_thumbnail with clip_id for the local artwork."
        )
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
        speaker = item.get("speaker") or ch().config().channel.speaker
        folder = Path(item["clip_path"]).parent
        item["thumbnail_path"] = str(
            thumbnails.draw(thumbnail_prompt, speaker, folder, ch().config())
        )
        item["thumbnail_prompt"] = thumbnail_prompt
    write_json_file(ch().queue_file, queue)
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
    a fresh illustration beside the speaker; view it after."""
    return await _sync(_edit_clip, clip_id, title, description, tags, thumbnail_prompt)


@tool(LOCAL_WRITE)
async def discard_clip(clip_id: str, reason: str) -> dict:
    """Drop a pending clip you never want to publish (weak topic, the wrong
    speaker, duplicate). Nothing on YouTube changes."""
    queue = _queue()
    item = _pending(queue, clip_id)
    item["status"] = "discarded"
    item["discard_reason"] = reason
    write_json_file(ch().queue_file, queue)
    Path(item["clip_path"]).unlink(missing_ok=True)
    return _queue_view(item)


@tool(WRITE)
async def publish_clip(clip_id: str, publish_at: str | None = None) -> dict:
    """Upload a pending clip, or retry a failed one after checking YouTube
    for an upload whose completion response was lost (without duplicating it).
    With `publish_at` (ISO 8601, e.g.
    2026-09-29T14:00:00Z) it goes up private and YouTube publishes it then;
    without it, it goes public as soon as it uploads. Queued uploads run one
    after another. Returns a job; follow it with wait_for_job."""
    item = _find_item(_queue(), clip_id)
    if item["status"] not in {"pending", "failed"}:
        raise ValueError(f"Clip is {item['status']}, not pending.")
    if any(j["args"]["item_id"] == clip_id for j in jobs.active(ch().name, "publish")):
        raise ValueError(f"{clip_id} is already queued for upload.")
    when = None
    if item["status"] == "failed" and publish_at is None:
        publish_at = item.get("publish_at")
    if publish_at:
        parsed = datetime.fromisoformat(publish_at)
        when = (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    return jobs.submit(
        ch().name,
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
    youtube = ch().youtube()
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
    baseline = performance.baseline(ch(), video_id)
    youtube.videos().update(
        part="snippet", body={"id": video_id, "snippet": body}
    ).execute()
    before = {key: snippet.get(key) for key in ("title", "description", "tags")}
    after = {key: body.get(key) for key in before}
    if before != after:
        performance.record_change(ch(), video_id, before, after, baseline)
    analytics.channel_videos(ch(), refresh=True)
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


def _set_video_thumbnail(
    video_id: str, thumbnail_prompt: str, speaker: str | None
) -> bytes:
    from googleapiclient.http import MediaFileUpload

    baseline = performance.baseline(ch(), video_id)

    uploaded = next((i for i in _queue() if i.get("youtube_id") == video_id), {})
    speaker = speaker or uploaded.get("speaker") or ch().config().channel.speaker
    thumbnail = thumbnails.draw(
        thumbnail_prompt,
        speaker,
        ch().dir / "video-thumbnails" / video_id,
        ch().config(),
    )
    ch().youtube().thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg"),
    ).execute()
    performance.record_change(
        ch(),
        video_id,
        {"thumbnail": "previous YouTube artwork"},
        {"thumbnail_prompt": thumbnail_prompt},
        baseline,
    )
    return thumbnail.read_bytes()


@tool(WRITE)
async def set_video_thumbnail(
    video_id: str, thumbnail_prompt: str, speaker: str | None = None
):
    """Draw a new thumbnail for a live channel video from `thumbnail_prompt` (a
    visual description of the idea) beside its speaker (known for videos clipy
    uploaded; otherwise pass `speaker`), set it on YouTube, and return it."""
    data = await _sync(_set_video_thumbnail, video_id, thumbnail_prompt, speaker)
    return Image(data=data, format="jpeg")


@tool(READ)
async def get_prompts() -> dict:
    """The prompts the pipeline uses right now: clip_extraction_prompt_template
    (picks clips, titles, descriptions, thumbnail prompts from a transcript;
    {speaker} is the source's main speaker), illustration_prompt_template
    (image model; {prompt} is the thumbnail prompt), and description_template
    ({clip_description}, {original_url})."""
    channel = ch().config().channel
    return {key: getattr(channel, key) for key in PROMPT_KEYS}


@tool(LOCAL_WRITE)
async def update_prompts(
    clip_extraction_prompt_template: str | None = None,
    illustration_prompt_template: str | None = None,
    description_template: str | None = None,
) -> dict:
    """Replace any of the pipeline prompts (see get_prompts). Applies to sources
    processed from now on. The clip prompt must still ask for the JSON shape
    {"clips": [{"title", "description", "start_ms", "end_ms", "score",
    "thumbnail_prompt"}]}; the description template must keep
    {clip_description} and {original_url}."""
    path = ch().prompt_overrides_file
    overrides = read_json_file(path) if path.exists() else {}
    changes = {
        "clip_extraction_prompt_template": clip_extraction_prompt_template,
        "illustration_prompt_template": illustration_prompt_template,
        "description_template": description_template,
    }
    overrides |= {key: value for key, value in changes.items() if value is not None}
    template = overrides.get("description_template")
    if template and not {"{clip_description}", "{original_url}"} <= set(
        re.findall(r"\{\w+\}", template)
    ):
        raise ValueError(
            "description_template needs {clip_description} and {original_url}."
        )
    if "{prompt}" not in overrides.get("illustration_prompt_template", "{prompt}"):
        raise ValueError("illustration_prompt_template needs {prompt}.")
    write_json_file(path, overrides)
    return await get_prompts()


@tool(READ)
async def read_notes() -> str:
    """Your strategy notebook from earlier runs: what was tried, what worked,
    experiments in flight, and the log of past runs. Read it first every run."""
    notes = ch().notes_file
    return notes.read_text(encoding="utf-8") if notes.exists() else "(empty)"


@tool(LOCAL_WRITE)
async def write_notes(content: str, append: bool = True) -> str:
    """Save to the strategy notebook. `append` adds a dated entry at the end;
    append=false replaces the whole notebook (use it to condense old entries
    when it gets long)."""
    notes = ch().notes_file
    notes.parent.mkdir(parents=True, exist_ok=True)
    if append:
        stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
        with notes.open("a", encoding="utf-8") as file:
            file.write(f"\n\n## {stamp}\n\n{content.strip()}\n")
    else:
        notes.write_text(content, encoding="utf-8")
    return f"Saved ({notes.stat().st_size} bytes)."


# --- entrypoint -------------------------------------------------------------


async def _healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def _releasez(_: Request) -> JSONResponse:
    from .delivery import readiness

    try:
        result = await _sync(readiness)
    except OSError, requests.RequestException, KeyError, ValueError:
        result = {"revision": os.environ.get("CLIPY_REVISION", ""), "ready": False}
    return JSONResponse(result, status_code=200 if result["ready"] else 503)


async def _stats(_: Request) -> PlainTextResponse:
    """The stats header of the daily report, for agent/run.sh."""
    return PlainTextResponse(await _sync(stats.report))


class _Gate:
    """Every request except /healthz needs `Authorization: Bearer <token>`,
    and /mcp/<channel> serves the MCP endpoint for that channel."""

    OPEN = frozenset({"/healthz", "/releasez"})
    GLOBAL = frozenset({"/stats"})

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.OPEN:
            await self.app(scope, receive, send)
            return
        supplied = dict(scope["headers"]).get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self.expected):
            await PlainTextResponse("unauthorized", 401)(scope, receive, send)
            return
        if scope["path"] in self.GLOBAL:
            await self.app(scope, receive, send)
            return
        name = scope["path"].removeprefix("/mcp/").strip("/")
        if name not in {c.name for c in all_channels()}:
            await PlainTextResponse("no such channel", 404)(scope, receive, send)
            return
        token = _channel.set(Channel(name))
        try:
            await self.app(scope | {"path": "/mcp", "raw_path": b"/mcp"}, receive, send)
        finally:
            _channel.reset(token)


def main() -> None:
    global jobs
    load_dotenv(PROJECT_ROOT / ".env")
    setup_logging()
    token = os.environ.get("CLIPY_MCP_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("CLIPY_MCP_TOKEN must be set to at least 32 characters.")

    for channel in all_channels():  # fail fast on a broken config.toml
        channel.config()
        channel.dir.mkdir(parents=True, exist_ok=True)
    # The agent writes speaker headshots here (its only writable data).
    (data_root() / "headshots").mkdir(exist_ok=True)
    jobs = JobRunner(
        data_root() / "jobs.json",
        {
            "process_source": _process_job,
            "publish": _publish_job,
            "thumbnails": _thumbnails_job,
            "repair": lambda name, args: recovery.repair(Channel(name), **args),
        },
    )
    performance.start_sampler()

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    app.router.routes += [
        Route("/healthz", _healthz),
        Route("/releasez", _releasez),
        Route("/stats", _stats),
    ]
    uvicorn.run(
        _Gate(app, token),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="warning",
    )


__all__ = ["main", "mcp"]


if __name__ == "__main__":
    main()
