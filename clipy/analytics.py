"""A channel's uploads and performance, from the YouTube Data and Analytics
APIs plus the reach reports (see reach.py)."""

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from . import reach
from .channel import Channel

logger = logging.getLogger(__name__)

DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
CACHE_SECONDS = 600
RANKINGS = {"revenue": "estimatedRevenue", "watch_hours": "estimatedMinutesWatched"}

_videos: dict[str, tuple[float, list[dict]]] = {}


def _seconds(iso_duration: str) -> int:
    match = DURATION_RE.fullmatch(iso_duration or "")
    if not match:
        return 0
    d, h, m, s = (int(g or 0) for g in match.groups())
    return ((d * 24 + h) * 60 + m) * 60 + s


def channel_videos(channel: Channel, refresh: bool = False) -> list[dict]:
    """Every upload on the channel newest first, including private and
    scheduled ones."""
    cached = _videos.get(channel.name)
    if not refresh and cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]

    youtube = channel.youtube()
    mine = youtube.channels().list(part="contentDetails", mine=True).execute()
    uploads = mine["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
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
            videos.append(
                {
                    "id": v["id"],
                    "title": v["snippet"]["title"],
                    "description": v["snippet"].get("description", ""),
                    "tags": v["snippet"].get("tags", []),
                    "privacy": status["privacyStatus"],
                    "upload_status": status.get("uploadStatus"),
                    "publish_time": status.get("publishAt")
                    or v["snippet"]["publishedAt"],
                    "duration_seconds": _seconds(v["contentDetails"]["duration"]),
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                }
            )
    videos.sort(key=lambda v: v["publish_time"], reverse=True)
    _videos[channel.name] = (time.monotonic(), videos)
    return videos


def _reach(channel: Channel) -> dict | str:
    """Impressions/CTR history, or why it is unavailable (never fatal)."""
    try:
        return reach.sync(channel.token_file, channel.reach_file)
    except Exception as exc:  # noqa: BLE001 - reports still work without CTR
        logger.warning("Reach reports unavailable: %s", exc)
        return f"unavailable: {exc}"


def _query(channel: Channel, **query: Any) -> list[dict]:
    response = (
        channel.analytics()
        .reports()
        .query(ids="channel==MINE", currency="USD", **query)
        .execute()
    )
    names = [h["name"] for h in response.get("columnHeaders", [])]
    return [dict(zip(names, row, strict=True)) for row in response.get("rows", [])]


def channel_report(channel: Channel, days: int, rank_by: str) -> dict:
    if rank_by not in RANKINGS:
        raise ValueError(f"rank_by is one of {sorted(RANKINGS)}.")
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    window = {"startDate": start.isoformat(), "endDate": end.isoformat()}
    daily = _query(
        channel,
        metrics="views,estimatedMinutesWatched,subscribersGained,estimatedRevenue",
        dimensions="day",
        sort="day",
        **window,
    )
    per_video = _query(
        channel,
        metrics=(
            "views,estimatedMinutesWatched,averageViewDuration,"
            "averageViewPercentage,subscribersGained,estimatedRevenue"
        ),
        dimensions="video",
        sort=f"-{RANKINGS[rank_by]}",
        maxResults=200,
        **window,
    )
    by_id = {v["id"]: v for v in channel_videos(channel, refresh=True)}
    reach_data = _reach(channel)
    ctr = reach.totals(reach_data, start, end) if isinstance(reach_data, dict) else {}
    videos = []
    for row in per_video:
        meta = by_id.get(row["video"], {})
        views = row["views"]
        videos.append(
            {
                "id": row["video"],
                "title": meta.get("title"),
                "published": meta.get("publish_time"),
                "duration_minutes": round(meta.get("duration_seconds", 0) / 60, 1),
                "lifetime_views": meta.get("views"),
                "views": views,
                "watch_hours": round(row["estimatedMinutesWatched"] / 60, 1),
                "revenue_usd": round(row["estimatedRevenue"], 2),
                "rpm_usd": round(row["estimatedRevenue"] / views * 1000, 2)
                if views
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
                "watch_hours": round(d["estimatedMinutesWatched"] / 60, 1),
                "revenue_usd": round(d["estimatedRevenue"], 2),
            }
            for d in daily
        ],
        "videos": videos,
    }


def video_report(channel: Channel, video_id: str, days: int) -> dict:
    end = datetime.now(UTC).date()
    window = {
        "startDate": (end - timedelta(days=days)).isoformat(),
        "endDate": end.isoformat(),
        "filters": f"video=={video_id}",
    }
    retention = _query(
        channel,
        metrics="audienceWatchRatio,relativeRetentionPerformance",
        dimensions="elapsedVideoTimeRatio",
        **window,
    )
    traffic = _query(
        channel,
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
        sort="-views",
        **window,
    )
    reach_data = _reach(channel)
    return {
        "video": next(
            (v for v in channel_videos(channel) if v["id"] == video_id), None
        ),
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
        "impressions_daily": reach.daily(
            reach_data, video_id, end - timedelta(days=days)
        )
        if isinstance(reach_data, dict)
        else reach_data,
    }
