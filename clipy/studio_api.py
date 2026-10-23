"""Raw channel-scoped Studio requests, with durable intent and no write retries."""

import uuid
from datetime import UTC, datetime

from .channel import Channel
from .common import write_json_file
from .studio_collect import StudioClient


def request(channel: Channel, endpoint: str, payload: dict, observation: dict | None):
    youtube = channel.youtube()
    channel_id = (
        youtube.channels().list(part="id", mine=True).execute()["items"][0]["id"]
    )
    video_ids = set()

    def targets(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"channelId", "externalChannelId"} and child != channel_id:
                    raise ValueError("Studio requests must stay within this channel.")
                if key in {"videoId", "encryptedVideoId"} and isinstance(child, str):
                    video_ids.add(child)
                elif key == "videoIds" and isinstance(child, list):
                    video_ids.update(child)
                targets(child)
        elif isinstance(value, list):
            for child in value:
                targets(child)

    targets(payload)
    ids = sorted(video_ids)
    for offset in range(0, len(ids), 50):
        batch = ids[offset : offset + 50]
        videos = (
            youtube.videos()
            .list(part="snippet", id=",".join(batch))
            .execute()
            .get("items", [])
        )
        if {v["id"] for v in videos if v["snippet"]["channelId"] == channel_id} != set(
            batch
        ):
            raise ValueError("Every target video must belong to this channel.")

    identity = uuid.uuid4().hex
    path = channel.dir / "studio-api" / f"{identity}.json"
    audit = {
        "id": identity,
        "endpoint": endpoint,
        "payload": payload,
        "observation": observation,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "started",
    }
    write_json_file(path, audit)
    client = StudioClient()
    try:
        response = client.request(channel_id, endpoint, payload)
    except Exception as exc:
        audit.update(status="unconfirmed", error=f"{type(exc).__name__}: {exc}")
        write_json_file(path, audit)
        raise
    finally:
        client.close()
    audit.update(
        status="responded", finished_at=datetime.now(UTC).isoformat(), response=response
    )
    write_json_file(path, audit)
    return {"response": response, "audit_id": identity}
