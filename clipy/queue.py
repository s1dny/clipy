import logging
from datetime import UTC, datetime
from pathlib import Path

from .clips import ClipSelection
from .common import read_json_file, write_json_file

logger = logging.getLogger(__name__)


def load_queue(queue_file: Path) -> list[dict]:
    """The channel's clip queue; a missing file is an empty queue."""
    return read_json_file(queue_file) if queue_file.exists() else []


def save_queue(clips: list[dict], queue_file: Path) -> None:
    """Save clips to the upload queue."""
    logger.info("[6/6] Saving to upload queue...")

    queue = load_queue(queue_file)

    queue_by_clip_path = {q.get("clip_path"): q for q in queue if q.get("clip_path")}
    for clip in clips:
        clip_path = clip.get("clip_path")
        if not clip_path:
            continue

        existing = queue_by_clip_path.get(clip_path)
        if existing:
            if clip.get("thumbnail_path"):
                existing["thumbnail_path"] = clip["thumbnail_path"]
            continue

        item = {
            "title": clip["title"],
            "speaker": clip.get("speaker"),
            "description": clip["description"],
            "clip_path": clip_path,
            "thumbnail_path": clip.get("thumbnail_path"),
            "thumbnail_prompt": clip.get("thumbnail_prompt"),
            "chapters": clip.get("chapters", []),
            "score": clip.get("score"),
            "start_ms": clip["start_ms"],
            "end_ms": clip["end_ms"],
            "duration_seconds": round(
                ClipSelection.model_validate(clip).duration_ms / 1000
            ),
            "edited": any(
                clip.get(key)
                for key in ("keep", "annotations", "visual_holds", "source")
            ),
            "source": clip.get("source"),
            "status": "pending",
            "created_at": datetime.now(UTC).isoformat(),
        }
        queue.append(item)
        queue_by_clip_path[clip_path] = item

    write_json_file(queue_file, queue)

    logger.info("Queue saved: %s", queue_file)
    logger.info("Total clips in queue: %s", len(queue))


def item_id(item: dict) -> str:
    """Stable queue id: '<source video id>:<clip number>' from the clip path."""
    parts = Path(item["clip_path"]).parts
    clips_idx = parts.index("clips")
    return f"{parts[clips_idx - 1]}:{parts[clips_idx + 1][:2]}"


def reconcile_uploads(queue_file: Path, videos: list[dict]) -> None:
    """Refresh local upload state and reclaim media only after processing succeeds."""
    by_id = {video["id"]: video for video in videos}
    queue = load_queue(queue_file)
    changed = False
    for item in queue:
        video = by_id.get(item.get("youtube_id"))
        if video is None:
            continue
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
        write_json_file(queue_file, queue)
