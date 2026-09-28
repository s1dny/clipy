"""A channel: its directory in the data root, its config, and its YouTube
account. Everything clipy keeps for a channel lives in
<data root>/channels/<name>/."""

import os
from dataclasses import dataclass
from pathlib import Path

from .common import extract_video_id
from .config import Config, channel_names, load_config
from .upload import get_analytics_service, get_youtube_service

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """Where channels and bot state live: $CLIPY_DATA_DIR, else the repo."""
    return Path(os.environ.get("CLIPY_DATA_DIR") or PROJECT_ROOT)


def config_path() -> Path:
    return Path(os.environ.get("CLIPY_CONFIG") or PROJECT_ROOT / "config.toml")


def all_channels() -> list[Channel]:
    return [Channel(name) for name in channel_names(config_path())]


@dataclass(frozen=True)
class Channel:
    name: str

    @property
    def dir(self) -> Path:
        return data_root() / "channels" / self.name

    @property
    def queue_file(self) -> Path:
        return self.dir / "upload_queue.json"

    @property
    def token_file(self) -> Path:
        return self.dir / ".youtube_token.json"

    @property
    def notes_file(self) -> Path:
        return self.dir / "notes.md"

    @property
    def prompt_overrides_file(self) -> Path:
        return self.dir / "prompt_overrides.json"

    @property
    def reach_file(self) -> Path:
        return self.dir / "reach.json"

    def config(self) -> Config:
        """Read fresh each time, so prompt overrides apply at once."""
        return load_config(config_path(), self.name, self.prompt_overrides_file)

    def source_dir(self, url: str) -> Path:
        return self.dir / extract_video_id(url)

    def youtube(self):
        return get_youtube_service(self.token_file)

    def analytics(self):
        return get_analytics_service(self.token_file)
