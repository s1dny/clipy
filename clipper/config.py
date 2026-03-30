import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ChannelConfig:
    description_template: str
    clip_extraction_prompt_template: str
    illustration_prompt_template: str


def _read_config_file(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    with config_path.open("rb") as file:
        return tomllib.load(file)


def _get_template_value(
    channel_data: dict[str, Any], defaults: dict[str, Any], key: str
) -> str:
    value = channel_data.get(key, defaults.get(key))
    if value is None:
        raise ValueError(
            f"Missing config.toml template '{key}' in [defaults] or [channels.*]"
        )
    if not isinstance(value, str):
        raise ValueError(f"config.toml field '{key}' must be a string")
    return value


def load_channel_config(project_root: Path, channel: str) -> ChannelConfig:
    config_path = project_root / "config.toml"
    data = _read_config_file(config_path)
    defaults = data.get("defaults", {})
    channels = data.get("channels", {})

    if defaults and not isinstance(defaults, dict):
        raise ValueError("config.toml [defaults] section must be a table")
    if channels and not isinstance(channels, dict):
        raise ValueError("config.toml [channels] section must be a table")

    channel_data = channels.get(channel, {})
    if channel_data and not isinstance(channel_data, dict):
        raise ValueError(f"config.toml [channels.{channel}] section must be a table")

    return ChannelConfig(
        description_template=_get_template_value(
            channel_data,
            defaults,
            "description_template",
        ),
        clip_extraction_prompt_template=_get_template_value(
            channel_data,
            defaults,
            "clip_extraction_prompt_template",
        ),
        illustration_prompt_template=_get_template_value(
            channel_data,
            defaults,
            "illustration_prompt_template",
        ),
    )
