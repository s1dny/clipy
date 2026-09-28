import os
from dataclasses import dataclass
from pathlib import Path

from .common import extract_video_id

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """Where channels and bot state live: $CLIPY_DATA_DIR, else the repo."""
    return Path(os.environ.get("CLIPY_DATA_DIR") or PROJECT_ROOT)


@dataclass(frozen=True)
class ClipyPaths:
    """Centralized project and work directory layout."""

    project_root: Path
    work_root: Path

    @property
    def queue_file(self) -> Path:
        return self.work_root / "upload_queue.json"

    @property
    def token_file(self) -> Path:
        return self.work_root / ".youtube_token.json"

    @property
    def notes_file(self) -> Path:
        return self.work_root / "notes.md"

    @property
    def jobs_file(self) -> Path:
        return self.work_root / "jobs.json"

    @property
    def prompt_overrides_file(self) -> Path:
        return self.work_root / "prompt_overrides.json"

    @property
    def reach_file(self) -> Path:
        return self.work_root / "reach.json"

    def work_dir_for_url(self, url: str) -> Path:
        return self.work_root / extract_video_id(url)

    def thumbnail_template(self, filename: str) -> Path:
        path = self.project_root / "thumbnails" / filename
        if not path.exists():
            raise FileNotFoundError(f"Thumbnail template not found: {path}")
        return path
