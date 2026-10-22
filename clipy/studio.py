"""Provenanced Studio readings, kept apart from delayed public API reports."""

import math
from datetime import UTC, datetime, timedelta

from .analytics import channel_videos
from .channel import Channel
from .common import read_json_file, write_json_file

METRICS = {
    "views",
    "impressions",
    "ctr_percent",
    "watch_hours",
    "subscribers",
    "revenue_usd",
}


def record(channel: Channel, readings: list[dict], *, source: str) -> dict:
    known = {v["id"] for v in channel_videos(channel, refresh=True)}
    now = datetime.now(UTC)
    clean = []
    for reading in readings:
        video_id = reading["video_id"]
        if video_id not in known:
            raise ValueError(f"Video {video_id} does not belong to this channel.")
        observed = datetime.fromisoformat(reading["observed_at"])
        if observed.tzinfo is None or not timedelta(0) <= now - observed <= timedelta(
            hours=2
        ):
            raise ValueError(
                "Studio readings need an explicit timezone and must be captured within the last two hours."
            )
        if reading.get("period") != "since_published":
            raise ValueError(
                "Capture Studio's Since published period; do not mix reporting windows."
            )
        url = reading["url"]
        if not url.startswith(
            f"https://studio.youtube.com/video/{video_id}/analytics/"
        ):
            raise ValueError("A matching Studio video-analytics URL is required.")
        metrics = reading["metrics"]
        if metrics.get("revenue_usd") is not None and reading.get("currency") != "USD":
            raise ValueError(
                "Revenue readings must explicitly identify currency as USD."
            )
        if not metrics or not set(metrics) <= METRICS:
            raise ValueError(f"Metrics must be a nonempty subset of {sorted(METRICS)}.")
        for key, value in metrics.items():
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{key} must be a finite number or null.")
            if value is not None and key != "subscribers" and value < 0:
                raise ValueError(f"{key} cannot be negative.")
            if key == "ctr_percent" and value is not None and value > 100:
                raise ValueError("ctr_percent is a percentage from 0 to 100.")
        clean.append(
            {
                **reading,
                "source": source,
                "observed_at": observed.astimezone(UTC).isoformat(),
                "approximate": bool(reading.get("approximate", True)),
            }
        )
    path = channel.dir / "studio.json"
    history = read_json_file(path) if path.exists() else []
    history = [
        r
        for r in history
        if now - datetime.fromisoformat(r["observed_at"]) < timedelta(days=35)
    ]
    identities = {(r["video_id"], r["observed_at"]) for r in history}
    history += [r for r in clean if (r["video_id"], r["observed_at"]) not in identities]
    write_json_file(path, history)
    return {"saved": len(clean), "channel": channel.name}


def latest(channel: Channel) -> dict:
    path = channel.dir / "studio.json"
    history = read_json_file(path) if path.exists() else []
    videos = {}
    for reading in sorted(history, key=lambda r: r["observed_at"]):
        metrics = dict(reading["metrics"])
        age = (
            datetime.now(UTC) - datetime.fromisoformat(reading["observed_at"])
        ).total_seconds() / 3600
        videos[reading["video_id"]] = {
            **reading,
            "metrics": metrics,
            "age_hours": round(age, 2),
            "stale": age > 26,
        }
    status_path = channel.dir / "studio-status.json"
    channel_path = channel.dir / "studio-channel.json"
    channel_reading = read_json_file(channel_path) if channel_path.exists() else None
    if channel_reading is not None:
        age = (
            datetime.now(UTC) - datetime.fromisoformat(channel_reading["observed_at"])
        ).total_seconds() / 3600
        channel_reading = {
            **channel_reading,
            "age_hours": round(age, 2),
            "stale": age > 2,
        }
    return {
        "channel": channel_reading,
        "collector": read_json_file(status_path)
        if status_path.exists()
        else {"status": "not collected yet"},
        "source": "YouTube Studio analytics",
        "note": "Since-published readings, separate from public API windows. Each reading identifies its source and whether it is approximate. Stale readings are not fresh experiment evidence. Missing metrics stay unknown.",
        "videos": list(videos.values()),
    }
