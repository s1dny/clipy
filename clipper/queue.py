import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("clipper")


def save_queue(clips: list[dict], work_root: Path, upload_thumbnail: str | None = None):
    """Save clips to the global upload queue in the output directory."""
    logger.info("[6/6] Saving to upload queue...")

    queue_file = work_root / "upload_queue.json"

    queue = []
    if queue_file.exists():
        with open(queue_file) as f:
            queue = json.load(f)

    queue_by_clip_path = {q.get("clip_path"): q for q in queue if q.get("clip_path")}
    for clip in clips:
        clip_path = clip.get("clip_path")
        if not clip_path:
            continue

        existing = queue_by_clip_path.get(clip_path)
        if existing:
            if upload_thumbnail:
                existing["upload_thumbnail"] = upload_thumbnail
            continue

        item = {
            "title": clip["title"],
            "description": clip["description"],
            "clip_path": clip_path,
            "thumbnail_path": clip.get("thumbnail_path"),
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if upload_thumbnail:
            item["upload_thumbnail"] = upload_thumbnail
        queue.append(item)
        queue_by_clip_path[clip_path] = item

    with open(queue_file, "w") as f:
        json.dump(queue, f, indent=2)

    logger.info(f"Queue saved: {queue_file}")
    logger.info(f"Total clips in queue: {len(queue)}")


def resolve_upload_thumbnail_path(filename: str) -> Path:
    """Resolve upload thumbnail filename from local thumbnail directories."""
    thumbnail_dirs = [Path.cwd() / "thumbnails", Path.cwd() / "thumbnail"]
    for directory in thumbnail_dirs:
        candidate = directory / filename
        if candidate.exists():
            return candidate

    searched = ", ".join(str(directory / filename) for directory in thumbnail_dirs)
    raise FileNotFoundError(f"Thumbnail not found. Looked for: {searched}")
