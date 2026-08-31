from pathlib import Path

import pytest

from clipper.config import load_config

CONFIG = """
[runtime]
ffmpeg_path = "ffmpeg-custom"

[models]
clip = "clip-model"
image = "image-model"
speech = "speech-model"

[thumbnails]
illustration_size = [640, 480]

[youtube]
publish_hour_utc = 9
category_id = "27"

[defaults]
description_template = "{clip_description} {original_url}"
clip_extraction_prompt_template = "Extract clips"
illustration_prompt_template = "Draw {prompt}"
illustration_center_x = 100
illustration_center_y = 200

[channels.example]
illustration_center_x = 300
"""


def write_config(project_root: Path, content: str = CONFIG) -> None:
    (project_root / "config.toml").write_text(content, encoding="utf-8")


def test_load_config_merges_channel_settings(tmp_path: Path) -> None:
    write_config(tmp_path)

    config = load_config(tmp_path, "example")

    assert config.runtime.ffmpeg_path == "ffmpeg-custom"
    assert config.models.clip == "clip-model"
    assert config.models.image == "image-model"
    assert config.models.speech == "speech-model"
    assert config.thumbnails.illustration_size == (640, 480)
    assert config.channel.illustration_center_x == 300
    assert config.channel.illustration_center_y == 200


def test_load_config_rejects_invalid_publish_hour(tmp_path: Path) -> None:
    write_config(
        tmp_path, CONFIG.replace("publish_hour_utc = 9", "publish_hour_utc = 24")
    )

    with pytest.raises(ValueError, match="between 0 and 23"):
        load_config(tmp_path, "example")


def test_load_config_requires_config_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Configuration file not found"):
        load_config(tmp_path, "example")
