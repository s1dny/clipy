"""Repair only Clipy-owned, unwatched private uploads; persist each transition."""

import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .channel import Channel
from .common import read_json_file, write_json_file
from .cutting import cut_clip
from .download import download_audio, download_video
from .queue import item_id, load_queue
from .upload import upload_video

PARTS = "snippet,status,statistics,contentDetails,processingDetails"
STALE_AFTER = timedelta(hours=24)


def _private_status(original: dict, publish_at: str | None = None) -> dict:
    status = {
        key: original[key]
        for key in (
            "license",
            "embeddable",
            "publicStatsViewable",
            "selfDeclaredMadeForKids",
        )
        if key in original
    }
    status["privacyStatus"] = "private"
    if publish_at:
        status["publishAt"] = publish_at
    return status


def inspect(youtube, video_id: str) -> dict:
    items = youtube.videos().list(part=PARTS, id=video_id).execute().get("items", [])
    if not items:
        raise ValueError(f"Video {video_id} is missing; do not guess its state.")
    return items[0]


def processed(video: dict) -> bool:
    from .analytics import _seconds

    return (
        video.get("processingDetails", {}).get("processingStatus") == "succeeded"
        or video["status"].get("uploadStatus") == "processed"
    ) and _seconds(video.get("contentDetails", {}).get("duration", "")) > 0


def _check_slot(channel: Channel, target: datetime, excluded: set[str]) -> None:
    from .analytics import channel_videos

    sydney = ZoneInfo("Australia/Sydney")
    day = target.astimezone(sydney).date()
    for video in channel_videos(channel, refresh=True):
        if (
            video["id"] not in excluded
            and datetime.fromisoformat(video["publish_time"]).astimezone(sydney).date()
            == day
        ):
            raise ValueError(
                f"Another upload already occupies Sydney day {day}; choose an empty slot."
            )


def _safe(video: dict) -> None:
    status = video["status"]
    processing = video.get("processingDetails", {})
    if status["privacyStatus"] != "private" or int(
        video.get("statistics", {}).get("viewCount", 0)
    ):
        raise ValueError("Only private uploads with zero views can be replaced.")
    if status.get("uploadStatus") == "rejected" or status.get("rejectionReason"):
        raise ValueError(
            "Rejected uploads need review; never re-upload to bypass a rejection."
        )
    if processed(video):
        raise ValueError(
            "Video has processed successfully; it does not need replacement."
        )
    failed = status.get("uploadStatus") == "failed" or processing.get(
        "processingStatus"
    ) in {"failed", "terminated"}
    uploaded = datetime.fromisoformat(video["snippet"]["publishedAt"])
    stale = (
        processing.get("processingStatus") == "processing"
        and datetime.now(UTC) - uploaded >= STALE_AFTER
    )
    if not (failed or stale):
        raise ValueError(
            "Processing is not failed or stalled for at least 24 hours; wait."
        )


def save_item(channel: Channel, item: dict) -> None:
    queue = load_queue(channel.queue_file)
    index = next(i for i, old in enumerate(queue) if item_id(old) == item_id(item))
    queue[index] = item
    write_json_file(channel.queue_file, queue)


def _rebuild(channel: Channel, item: dict) -> None:
    source, number = item_id(item).split(":")
    clips = read_json_file(
        channel.source_dir(f"https://youtube.com/watch?v={source}") / "clips.json"
    )
    clip = clips[int(number) - 1]
    output = Path(item["clip_path"])
    output.parent.mkdir(parents=True, exist_ok=True)
    # Full encoding avoids carrying the original MP4's stream/timestamp faults.
    with tempfile.TemporaryDirectory(dir=output.parent) as work:
        folder = Path(work)
        url = f"https://youtube.com/watch?v={source}"
        audio = download_audio(url, folder)
        video = download_video(url, folder)
        if not cut_clip(
            video,
            audio,
            clip["start_ms"],
            clip["end_ms"],
            output,
            channel.config().ffmpeg,
        ):
            raise RuntimeError(
                "Could not rebuild the clip; original upload is unchanged."
            )

    item["cut_encoding"] = "full"
    item["start_ms"] = clip["start_ms"]
    item["end_ms"] = clip["end_ms"]


def repair(channel: Channel, video_id: str, publish_at: str | None = None) -> str:
    youtube = channel.youtube()
    item = next(
        (
            i
            for i in load_queue(channel.queue_file)
            if i.get("youtube_id") == video_id
            or i.get("repair", {}).get("original_id") == video_id
        ),
        None,
    )
    if item is None:
        raise ValueError(
            "Only uploads linked to this channel's Clipy queue can be repaired."
        )
    state = item.get("repair")
    if state and state.get("completed_at"):
        return item["youtube_url"]
    if state is None:
        original = inspect(youtube, video_id)
        _safe(original)
        when = publish_at or original["status"].get("publishAt")
        if not when:
            raise ValueError("Supply a future publish_at for this unscheduled upload.")
        target = datetime.fromisoformat(when)
        if target.tzinfo is None or not timedelta(hours=12) <= target - datetime.now(
            UTC
        ) <= timedelta(days=8):
            raise ValueError(
                "Replacement publish_at needs a timezone and must be 12 hours to 8 days ahead."
            )
        _check_slot(channel, target, {video_id})
        state = item["repair"] = {
            "original_id": video_id,
            "publish_at": target.astimezone(UTC).isoformat(),
            "started_at": datetime.now(UTC).isoformat(),
            "snippet": original["snippet"],
            "original_status": original["status"],
        }
        save_item(channel, item)
    if not state.get("replacement_id"):
        _safe(inspect(youtube, state["original_id"]))
        if state.get("upload_started"):
            # Upload completion can be lost even after all session retries. Find
            # the replacement by a persisted unique tag before ever inserting.
            from .analytics import channel_videos

            matches = [
                v
                for v in channel_videos(channel, refresh=True)
                if state["marker"] in v["tags"]
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Replacement upload outcome is uncertain; inspect YouTube before retrying."
                )
            state["replacement_id"] = matches[0]["id"]
            save_item(channel, item)
        else:
            _rebuild(channel, item)
            state["marker"] = f"clipy-repair-{state['original_id']}"
            state["upload_started"] = True
            save_item(channel, item)
            replacement = {
                **item,
                "title": state["snippet"]["title"],
                "tags": [*state["snippet"].get("tags", []), state["marker"]],
            }
            replacement.pop("publish_at", None)
            replacement.pop("youtube_id", None)

            def remember(uploaded):
                state["replacement_id"] = uploaded["youtube_id"]
                item["thumbnail_pending"] = True
                save_item(channel, item)

            # Preserve the live description, including changes made since upload.
            template = (
                state["snippet"]["description"].replace("{", "{{").replace("}", "}}")
            )
            upload_video(
                youtube,
                replacement,
                template,
                state["snippet"]["categoryId"],
                private=True,
                on_uploaded=remember,
            )
            item["thumbnail_pending"] = replacement.get("thumbnail_pending", False)
            save_item(channel, item)
    replacement_id = state["replacement_id"]
    deadline = time.monotonic() + 10 * 60
    while True:
        replacement = inspect(youtube, replacement_id)
        if processed(replacement):
            break
        if replacement["status"].get("uploadStatus") in {
            "failed",
            "rejected",
        } or replacement.get("processingDetails", {}).get("processingStatus") in {
            "failed",
            "terminated",
        }:
            raise RuntimeError(
                "Replacement processing failed; originals and local media are retained."
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Replacement is still processing; call repair_upload again to resume this same replacement."
            )
        time.sleep(15)
    # Finish only while both copies are private and unwatched. Persist the new
    # identity before deletion so a lost delete response never triggers re-upload.
    if not state.get("activated"):
        original = inspect(youtube, state["original_id"])
        _safe(original)
        if replacement["status"]["privacyStatus"] != "private" or int(
            replacement.get("statistics", {}).get("viewCount", 0)
        ):
            raise ValueError(
                "Replacement is no longer private and unwatched; inspect before proceeding."
            )
        if datetime.fromisoformat(state["publish_at"]) <= datetime.now(UTC):
            raise ValueError(
                "The release slot has passed; inspect and choose a new slot before activating."
            )
        _check_slot(
            channel,
            datetime.fromisoformat(state["publish_at"]),
            {state["original_id"], replacement_id},
        )
        youtube.videos().update(
            part="status",
            body={
                "id": state["original_id"],
                "status": _private_status(original["status"]),
            },
        ).execute()
        youtube.videos().update(
            part="snippet,status",
            body={
                "id": replacement_id,
                "snippet": {
                    k: state["snippet"][k]
                    for k in ("title", "description", "categoryId", "tags")
                    if k in state["snippet"]
                },
                "status": _private_status(
                    state.get("original_status", original["status"]),
                    state["publish_at"],
                ),
            },
        ).execute()
        item.update(
            youtube_id=replacement_id,
            youtube_url=f"https://youtube.com/watch?v={replacement_id}",
            publish_at=state["publish_at"],
            status="uploaded",
            processing_status="succeeded",
        )
        state["activated"] = True
        save_item(channel, item)
    old = (
        youtube.videos()
        .list(part=PARTS, id=state["original_id"])
        .execute()
        .get("items", [])
    )
    if old:
        _safe(old[0])
        if not processed(inspect(youtube, replacement_id)):
            raise ValueError(
                "Replacement processing is no longer successful; retain original."
            )
        youtube.videos().delete(id=state["original_id"]).execute()
    state["completed_at"] = datetime.now(UTC).isoformat()
    save_item(channel, item)
    Path(item["clip_path"]).unlink(missing_ok=True)
    return item["youtube_url"]
