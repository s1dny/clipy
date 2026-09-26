import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when config.toml is missing or invalid."""


@dataclass(frozen=True)
class RuntimeConfig:
    ffmpeg_path: str


@dataclass(frozen=True)
class ModelsConfig:
    clip: str
    image: str
    speech: str
    assemblyai_usd_per_hour: float


@dataclass(frozen=True)
class ThumbnailConfig:
    illustration_size: tuple[int, int]


@dataclass(frozen=True)
class YouTubeConfig:
    category_id: str


@dataclass(frozen=True)
class ChannelConfig:
    description_template: str
    clip_extraction_prompt_template: str
    illustration_prompt_template: str
    illustration_center_x: int
    illustration_center_y: int
    thumbnail_template: str


PROMPT_KEYS = (
    "clip_extraction_prompt_template",
    "illustration_prompt_template",
    "description_template",
)


@dataclass(frozen=True)
class AppConfig:
    runtime: RuntimeConfig
    models: ModelsConfig
    thumbnails: ThumbnailConfig
    youtube: YouTubeConfig
    channel: ChannelConfig


def _read_config_file(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"config.toml [{key}] must be a table")
    return value


def _value(
    data: dict[str, Any], key: str, expected_type: type, default: Any = None
) -> Any:
    value = data.get(key, default)
    if value is None:
        raise ConfigError(f"Missing config.toml field '{key}'")
    if not isinstance(value, expected_type) or (
        expected_type is int and isinstance(value, bool)
    ):
        raise ConfigError(
            f"config.toml field '{key}' must be {getattr(expected_type, '__name__', expected_type)}"
        )
    return value


def _int_pair(data: dict[str, Any], key: str, default: list[int]) -> tuple[int, int]:
    value = _value(data, key, list, default)
    if (
        len(value) != 2
        or not all(
            isinstance(item, int) and not isinstance(item, bool) for item in value
        )
        or not all(item > 0 for item in value)
    ):
        raise ConfigError(f"config.toml field '{key}' must be two positive integers")
    return value[0], value[1]


def _read_overrides(overrides_path: Path | None) -> dict[str, str]:
    if overrides_path is None or not overrides_path.exists():
        return {}
    try:
        data = json.loads(overrides_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {overrides_path}: {exc}") from exc
    return {k: v for k, v in data.items() if k in PROMPT_KEYS and isinstance(v, str)}


def _channel_config(
    data: dict[str, Any], channel: str, overrides: dict[str, str]
) -> ChannelConfig:
    defaults = _table(data, "defaults")
    channels = _table(data, "channels")
    channel_data = channels.get(channel, {})
    if not isinstance(channel_data, dict):
        raise ConfigError(f"config.toml [channels.{channel}] must be a table")

    merged = defaults | channel_data | overrides
    return ChannelConfig(
        description_template=_value(merged, "description_template", str),
        clip_extraction_prompt_template=_value(
            merged, "clip_extraction_prompt_template", str
        ),
        illustration_prompt_template=_value(
            merged, "illustration_prompt_template", str
        ),
        illustration_center_x=_value(merged, "illustration_center_x", int, 850),
        illustration_center_y=_value(merged, "illustration_center_y", int, 350),
        thumbnail_template=_value(merged, "thumbnail_template", str),
    )


def load_config(
    config_path: Path, channel: str, overrides_path: Path | None = None
) -> AppConfig:
    """Load config.toml, then layer the channel's prompt overrides on top."""
    data = _read_config_file(config_path)
    runtime = _table(data, "runtime")
    models = _table(data, "models")
    thumbnails = _table(data, "thumbnails")
    youtube = _table(data, "youtube")

    return AppConfig(
        runtime=RuntimeConfig(
            ffmpeg_path=_value(runtime, "ffmpeg_path", str, "ffmpeg"),
        ),
        models=ModelsConfig(
            clip=_value(models, "clip", str, "google/gemini-3.7-flash"),
            image=_value(models, "image", str, "google/gemini-3.1-flash-image"),
            speech=_value(models, "speech", str, "universal-3-5-pro"),
            assemblyai_usd_per_hour=float(
                _value(models, "assemblyai_usd_per_hour", int | float, 0.27)
            ),
        ),
        thumbnails=ThumbnailConfig(
            illustration_size=_int_pair(thumbnails, "illustration_size", [700, 700]),
        ),
        youtube=YouTubeConfig(
            category_id=_value(youtube, "category_id", str, "22"),
        ),
        channel=_channel_config(data, channel, _read_overrides(overrides_path)),
    )
