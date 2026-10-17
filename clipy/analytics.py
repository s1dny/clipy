"""Cached upload inventory for local queue recovery and counter collection."""

import logging
import re
import time

from .channel import Channel

logger = logging.getLogger(__name__)

DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
CACHE_SECONDS = 600

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
                part="snippet,statistics,contentDetails,status,processingDetails",
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
                    "processing_status": v.get("processingDetails", {}).get(
                        "processingStatus"
                    ),
                    "processing_failure_reason": v.get("processingDetails", {}).get(
                        "processingFailureReason"
                    ),
                    "failure_reason": status.get("failureReason"),
                    "rejection_reason": status.get("rejectionReason"),
                    "uploaded_at": v["snippet"]["publishedAt"],
                    "thumbnails": v["snippet"].get("thumbnails", {}),
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
