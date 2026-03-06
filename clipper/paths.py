from dataclasses import dataclass
from pathlib import Path

from .common import extract_video_id


@dataclass(frozen=True)
class ClipperPaths:
    """Centralized project and work directory layout."""

    project_root: Path
    work_root: Path

    @property
    def queue_file(self) -> Path:
        return self.work_root / "upload_queue.json"

    def work_dir_for_url(self, url: str) -> Path:
        return self.work_root / extract_video_id(url)

    def resolve_upload_thumbnail(self, filename: str) -> Path:
        thumbnail_dirs = (
            self.project_root / "thumbnails",
            self.project_root / "thumbnail",
        )
        for directory in thumbnail_dirs:
            candidate = directory / filename
            if candidate.exists():
                return candidate

        searched = ", ".join(str(directory / filename) for directory in thumbnail_dirs)
        raise FileNotFoundError(f"Thumbnail not found. Looked for: {searched}")
