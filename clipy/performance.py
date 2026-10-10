"""Hourly Data API counter snapshots, separate from delayed Analytics metrics."""

import logging
import threading
from datetime import UTC, datetime, timedelta

from .analytics import channel_videos
from .channel import Channel, all_channels
from .common import read_json_file, write_json_file

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
        current = history[-1]
        observed = datetime.fromisoformat(current["observed_at"])
        windows = {}
        for hours in (24, 48):
            target = observed - timedelta(hours=hours)
            baseline = min(
                history,
                key=lambda s: abs(datetime.fromisoformat(s["observed_at"]) - target),
            )
            windows[str(hours)] = (
                baseline
                if abs(datetime.fromisoformat(baseline["observed_at"]) - target)
                <= timedelta(hours=2)
                else None
            )
        videos = []
        for video_id, counts in current["videos"].items():
            deltas = {}
            for hours, baseline in windows.items():
                before = baseline["videos"].get(video_id) if baseline else None
                deltas[hours] = {
                    "baseline_at": baseline["observed_at"] if before else None,
                    **{
                        k: counts[k] - before[k] if before else None
                        for k in ("views", "likes", "comments")
                    },
                }
            videos.append({"id": video_id, **counts, "counter_changes": deltas})
        return {
            "observed_at": current["observed_at"],
            "source": "YouTube Data API counters (provisional, not realtime Analytics)",
            "subscribers": current["subscribers"],
            "subscriber_precision": "API rounds to three significant figures; small changes may be hidden.",
            "note": "24/48h counter changes need prior snapshots; null means unavailable. Counts may be corrected. These are not CTR, watch hours, revenue, or causal experiment results.",
            "videos": sorted(videos, key=lambda v: v["publish_time"], reverse=True),
        }


def start_sampler() -> None:
    def run():
        while True:
            for channel in all_channels():
                try:
                    recent(channel)
                except Exception:
                    logger.exception("Performance snapshot failed for %s", channel.name)
            threading.Event().wait(3600)

    threading.Thread(target=run, name="clipy-performance", daemon=True).start()


def baseline(channel: Channel, video_id: str) -> dict:
    from .studio import latest

    current = recent(channel)
    return {
        "observed_at": current["observed_at"],
        "counters": next((v for v in current["videos"] if v["id"] == video_id), None),
        "studio": next(
            (v for v in latest(channel)["videos"] if v["video_id"] == video_id), None
        ),
    }


def record_change(
    channel: Channel, video_id: str, before: dict, after: dict, observation: dict
) -> None:
    path = channel.dir / "experiments.json"
    history = read_json_file(path) if path.exists() else []
    now = datetime.now(UTC)
    history = [
        e
        for e in history
        if now - datetime.fromisoformat(e["changed_at"]) < timedelta(days=35)
    ]
    history.append(
        {
            "video_id": video_id,
            "changed_at": now.isoformat(),
            "before": before,
            "after": after,
            "baseline": observation,
        }
    )
    write_json_file(path, history)
