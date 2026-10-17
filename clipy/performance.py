"""Hourly Data API counter snapshots, separate from delayed Analytics metrics."""

import logging
import threading
from datetime import UTC, datetime, timedelta

from .analytics import channel_videos
from .channel import Channel, all_channels
from .common import read_json_file, write_json_file
from .queue import reconcile_uploads

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def recent(channel: Channel) -> dict:
    with _lock:
        path = channel.dir / "performance.json"
        history = read_json_file(path) if path.exists() else []
        now = datetime.now(UTC)
        if not history or now - datetime.fromisoformat(
            history[-1]["observed_at"]
        ) >= timedelta(minutes=10):
            videos = channel_videos(channel, refresh=True)
            mine = (
                channel.youtube()
                .channels()
                .list(part="statistics", mine=True)
                .execute()["items"][0]["statistics"]
            )
            history.append(
                {
                    "observed_at": now.isoformat(),
                    "subscribers": int(mine["subscriberCount"])
                    if "subscriberCount" in mine
                    else None,
                    "videos": {
                        v["id"]: {
                            k: v[k]
                            for k in (
                                "views",
                                "likes",
                                "comments",
                                "title",
                                "privacy",
                                "publish_time",
                            )
                        }
                        for v in videos
                    },
                }
            )
            history = [
                s
                for s in history
                if now - datetime.fromisoformat(s["observed_at"]) < timedelta(days=35)
            ]
            write_json_file(path, history)
        return history[-1]


def observations(channel: Channel, offset: int, limit: int) -> dict:
    """Raw observations, newest first; the agent chooses comparisons and windows."""
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("offset must be nonnegative and limit between 1 and 200.")
    recent(channel)
    history = read_json_file(channel.dir / "performance.json")[::-1]
    stop = min(offset + limit, len(history))
    return {
        "snapshots": history[offset:stop],
        "snapshot_count": len(history),
        "next_offset": stop if stop < len(history) else None,
    }


def start_sampler() -> None:
    def run():
        while True:
            for channel in all_channels():
                try:
                    recent(channel)
                    reconcile_uploads(channel.queue_file, channel_videos(channel))
                except Exception:
                    logger.exception("Performance snapshot failed for %s", channel.name)
            threading.Event().wait(3600)

    threading.Thread(target=run, name="clipy-performance", daemon=True).start()
