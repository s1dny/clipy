"""Thumbnails: a picture on the left, and on the white right side an
illustration of the clip's idea drawn by the image model.

The left is the channel's fixed template (thumbnails/<file>) when it has one,
otherwise the clip speaker's headshot (see headshots.py) with a soft shadow.
"""

import base64
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from . import headshots
from .channel import PROJECT_ROOT
from .common import openrouter_message, openrouter_request
from .config import Config
from .cutting import clip_folder_name

logger = logging.getLogger(__name__)

SIZE = (1920, 1080)
ILLUSTRATION_SIZE = (700, 700)
SHADOW = 80
WHITE = 240  # every channel at least this bright counts as white
IMAGE_TIMEOUT_SECONDS = 120
MAX_WORKERS = 10


def _left(config: Config, speaker: str | None) -> Image.Image:
    """The thumbnail before its illustration."""
    if config.channel.thumbnail_template:
        path = PROJECT_ROOT / "thumbnails" / config.channel.thumbnail_template
        return Image.open(path).convert("RGB").resize(SIZE)
    if not speaker:
        raise ValueError("This channel's thumbnails need the clip's speaker.")
    panel = headshots.panel(speaker)
    canvas = Image.new("RGB", SIZE, "white")
    shade = Image.new("L", SIZE, 0)
    shade.paste(255, (0, 0, panel.width + SHADOW // 3, SIZE[1]))
    shade = shade.filter(ImageFilter.GaussianBlur(SHADOW / 3))
    canvas.paste((150, 150, 150), (0, 0), shade)
    canvas.paste(panel, (0, 0))
    return canvas


def _illustration(prompt: str, config: Config) -> Image.Image:
    template = config.channel.illustration_prompt_template
    data = openrouter_request(
        {
            "model": config.models.image,
            "messages": [{"role": "user", "content": template.format(prompt=prompt)}],
            "modalities": ["image"],
            "provider": {"sort": "price"},
        },
        IMAGE_TIMEOUT_SECONDS,
    )
    for image in openrouter_message(data).get("images") or []:
        url = image.get("image_url", {}).get("url", "")
        if url.startswith("data:image"):
            png = base64.b64decode(url.split(",", 1)[1])
            return Image.open(io.BytesIO(png)).convert("RGBA")
    raise RuntimeError("The image model returned no image.")


def compose(left: Image.Image, illustration: Image.Image) -> Image.Image:
    """Crop the illustration to its non-white content, scale it, and centre it
    in the white area right of the picture."""
    pixels = np.array(illustration)
    ink = (pixels[:, :, 3] > 10) & (pixels[:, :, :3] < 220).any(axis=2)
    rows, cols = np.nonzero(ink)
    if not rows.size:
        raise RuntimeError("The illustration is blank.")
    drawn = illustration.crop((cols.min(), rows.min(), cols.max() + 1, rows.max() + 1))
    drawn.thumbnail(ILLUSTRATION_SIZE, Image.Resampling.LANCZOS)

    white = (np.array(left) >= WHITE).all(axis=(0, 2))
    start = len(white) - int(np.argmin(white[::-1])) if not white.all() else 0
    x = (start + SIZE[0]) // 2 - drawn.width // 2
    y = SIZE[1] // 2 - drawn.height // 2
    thumbnail = left.copy()
    thumbnail.paste(drawn, (x, y), drawn)
    return thumbnail


def draw(prompt: str, speaker: str | None, folder: Path, config: Config) -> Path:
    """Draw a thumbnail for `prompt` into folder/thumbnail.jpg."""
    left = _left(config, speaker)  # fails fast, before paying for an image
    thumbnail = compose(left, _illustration(prompt, config))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "thumbnail.jpg"
    thumbnail.save(path, "JPEG", quality=95)
    (folder / ".thumbnail_prompt").write_text(prompt, encoding="utf-8")
    return path


def draw_all(clips: list[dict], source_dir: Path, config: Config) -> list[dict]:
    """Each clip's thumbnail, drawn in parallel; one already drawn from the same
    prompt is kept. A clip whose thumbnail fails gets thumbnail_path None."""
    logger.info("[5/6] Drawing thumbnails...")

    def one(i: int) -> str | None:
        clip = clips[i]
        folder = source_dir / "clips" / clip_folder_name(i, clip["title"])
        prompt = clip.get("thumbnail_prompt", clip["title"]).strip()
        done = folder / ".thumbnail_prompt"
        if done.exists() and done.read_text(encoding="utf-8").strip() == prompt:
            return str(folder / "thumbnail.jpg")
        try:
            return str(draw(prompt, clip.get("speaker"), folder, config))
        except Exception as exc:  # noqa: BLE001 - the agent can redraw it
            logger.error("Thumbnail for %r failed: %s", clip["title"], exc)
            return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for clip, path in zip(clips, pool.map(one, range(len(clips))), strict=True):
            clip["thumbnail_path"] = path
    return clips
