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


@dataclass(frozen=True)
class ThumbnailConfig:
    illustration_size: tuple[int, int]


@dataclass(frozen=True)
class YouTubeConfig:
    publish_hour_utc: int
    category_id: str


@dataclass(frozen=True)
class ChannelConfig:
    description_template: str
    clip_extraction_prompt_template: str
    illustration_prompt_template: str
    illustration_center_x: int
    illustration_center_y: int


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
        raise ConfigError(f"config.toml field '{key}' must be {expected_type.__name__}")
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


def _channel_config(data: dict[str, Any], channel: str) -> ChannelConfig:
    defaults = _table(data, "defaults")
    channels = _table(data, "channels")
    channel_data = channels.get(channel, {})
    if not isinstance(channel_data, dict):
        raise ConfigError(f"config.toml [channels.{channel}] must be a table")

    merged = defaults | channel_data
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
    )


def load_config(project_root: Path, channel: str) -> AppConfig:
    """Load and validate application and channel settings from config.toml."""
    data = _read_config_file(project_root / "config.toml")
    runtime = _table(data, "runtime")
    models = _table(data, "models")
    thumbnails = _table(data, "thumbnails")
    youtube = _table(data, "youtube")

    publish_hour = _value(youtube, "publish_hour_utc", int, 12)
    if not 0 <= publish_hour <= 23:
        raise ConfigError(
            "config.toml field 'publish_hour_utc' must be between 0 and 23"
        )

    return AppConfig(
        runtime=RuntimeConfig(
            ffmpeg_path=_value(runtime, "ffmpeg_path", str, "ffmpeg"),
        ),
        models=ModelsConfig(
            clip=_value(models, "clip", str, "google/gemini-3.7-flash"),
            image=_value(models, "image", str, "google/gemini-3.1-flash-image"),
            speech=_value(models, "speech", str, "universal-3-5-pro"),
        ),
        thumbnails=ThumbnailConfig(
            illustration_size=_int_pair(thumbnails, "illustration_size", [700, 700]),
        ),
        youtube=YouTubeConfig(
            publish_hour_utc=publish_hour,
            category_id=_value(youtube, "category_id", str, "22"),
        ),
        channel=_channel_config(data, channel),
    )
