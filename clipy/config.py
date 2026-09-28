"""config.toml: models, ffmpeg, and each channel's settings and prompts.

A channel's settings are [defaults], then [channels.<name>] on top, then the
agent's prompt overrides (prompt_overrides.json in the channel's directory).
"""

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROMPT_KEYS = (
    "clip_extraction_prompt_template",
    "illustration_prompt_template",
    "description_template",
)


class ConfigError(ValueError):
    """Raised when config.toml is missing or invalid."""


@dataclass(frozen=True)
class Models:
    clip: str
    image: str
    speech: str


@dataclass(frozen=True)
class ChannelConfig:
    # The YouTube channel ("@ScienceClipsDaily") its token must belong to.
    handle: str
    # Every source's main speaker, for a channel about one person.
    speaker: str | None
    # A fixed thumbnail in thumbnails/; without one, thumbnails put the
    # speaker's headshot on the left.
    thumbnail_template: str | None
    category_id: str
    clip_extraction_prompt_template: str
    illustration_prompt_template: str
    description_template: str


@dataclass(frozen=True)
class Config:
    ffmpeg: str
    models: Models
    channel: ChannelConfig


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc


def _get(table: dict[str, Any], key: str, default: Any = ...) -> Any:
    value = table.get(key, default)
    if value is ...:
        raise ConfigError(f"Missing config.toml field '{key}'")
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"config.toml field '{key}' must be a string")
    return value


def channel_names(path: Path) -> list[str]:
    return list(_read(path).get("channels", {}))


def load_config(path: Path, channel: str, overrides_path: Path) -> Config:
    data = _read(path)
    if channel not in data.get("channels", {}):
        raise ConfigError(f"config.toml has no [channels.{channel}]")
    overrides = (
        json.loads(overrides_path.read_text(encoding="utf-8"))
        if overrides_path.exists()
        else {}
    )
    merged = (
        data.get("defaults", {})
        | data["channels"][channel]
        | {k: v for k, v in overrides.items() if k in PROMPT_KEYS}
    )
    models = data.get("models", {})
    return Config(
        ffmpeg=_get(data.get("runtime", {}), "ffmpeg_path", "ffmpeg"),
        models=Models(
            clip=_get(models, "clip", "z-ai/glm-5.3-flash"),
            image=_get(models, "image", "bytedance-seed/seedream-5-0-lite"),
            speech=_get(models, "speech", "universal-3-5-pro"),
        ),
        channel=ChannelConfig(
            handle=_get(merged, "handle"),
            speaker=_get(merged, "speaker", None),
            thumbnail_template=_get(merged, "thumbnail_template", None),
            category_id=_get(merged, "category_id", "22"),
            **{key: _get(merged, key) for key in PROMPT_KEYS},
        ),
    )
