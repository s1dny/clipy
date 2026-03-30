import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .common import write_json_file

logger = logging.getLogger("clipper")


def load_queue(queue_file: Path) -> list[dict]:
    """Load the upload queue, tolerating empty or invalid JSON files."""
    if not queue_file.exists():
        return []

    try:
        with queue_file.open(encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError as exc:
        logger.warning(f"Could not read queue file {queue_file}: {exc}")
        return []

    if not raw:
        logger.warning(f"Queue file is empty; treating as a new queue: {queue_file}")
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            f"Queue file is invalid JSON; resetting queue {queue_file}: {exc}"
        )
        return []

    if not isinstance(data, list):
        logger.warning(
            f"Queue file must contain a JSON array; resetting queue {queue_file}"
        )
        return []

    for item in data:
        if isinstance(item, dict):
            item.pop("upload_thumbnail", None)

    return data


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
            existing.pop("upload_thumbnail", None)
            continue

        item = {
            "title": clip["title"],
            "description": clip["description"],
            "clip_path": clip_path,
            "thumbnail_path": clip.get("thumbnail_path"),
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        queue.append(item)
        queue_by_clip_path[clip_path] = item

    write_json_file(queue_file, queue)

    logger.info(f"Queue saved: {queue_file}")
    logger.info(f"Total clips in queue: {len(queue)}")


def remove_work_dir_entries(queue_file: Path, work_dir: Path) -> None:
    """Remove queue entries produced by a specific work directory."""
    queue = [
        item
        for item in load_queue(queue_file)
        if not item.get("clip_path", "").startswith(str(work_dir))
    ]
    write_json_file(queue_file, queue)
