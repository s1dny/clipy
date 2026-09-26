import base64
import binascii
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from .clips import clip_folder_name
from .common import openrouter_message_images, openrouter_request
from .config import ChannelConfig, ThumbnailConfig

logger = logging.getLogger(__name__)
IMAGE_TIMEOUT_SECONDS = 120
MAX_WORKERS = 10


def thumbnail_prompt_text(clip: dict) -> str:
    """Normalize the prompt used both for generation and cache invalidation."""
    return clip.get("thumbnail_prompt", clip["title"]).strip()


def generate_illustration(
    prompt: str,
    output_path: Path,
    api_key: str,
    prompt_template: str,
    model: str,
) -> bool:
    """Generate an illustration to be composited onto thumbnail template."""
    logger.info("Generating illustration: %s...", output_path.name)

    try:
        illustration_prompt = prompt_template.format(prompt=prompt)
        data = openrouter_request(
            api_key,
            {
                "model": model,
                "messages": [{"role": "user", "content": illustration_prompt}],
                "modalities": ["image"],
                "provider": {"sort": "price"},
            },
            IMAGE_TIMEOUT_SECONDS,
        )

        images = openrouter_message_images(data)
        if images:
            img_data_url = images[0].get("image_url", {}).get("url", "")
            if img_data_url.startswith("data:image"):
                # Extract base64 data after the comma
                b64_data = img_data_url.split(",", 1)[1]
                img_bytes = base64.b64decode(b64_data)
                image = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
                image.save(output_path, "PNG")
                return True

        logger.error("No image in OpenRouter response")
        return False

    except (
        binascii.Error,
        KeyError,
        OSError,
        RuntimeError,
        ValueError,
        requests.RequestException,
    ) as exc:
        logger.error("Illustration generation failed: %s", exc)
        return False


def composite_thumbnail(
    illustration_path: Path,
    output_path: Path,
    template_path: Path,
    center_x: int,
    center_y: int,
    illustration_size: tuple[int, int],
) -> bool:
    """Crop illustration to non-white content, scale, and center on template."""
    logger.info("Compositing thumbnail: %s...", output_path.name)

    if not template_path.exists():
        logger.error("Thumbnail template not found at %s", template_path)
        return False

    try:
        template = Image.open(template_path).convert("RGBA")
        illustration = Image.open(illustration_path).convert("RGBA")

        # Find bounding box of non-white pixels using numpy
        arr = np.array(illustration)
        threshold = 220
        # Mask: alpha > 10 and any RGB channel below threshold
        mask = (arr[:, :, 3] > 10) & (
            (arr[:, :, 0] < threshold)
            | (arr[:, :, 1] < threshold)
            | (arr[:, :, 2] < threshold)
        )
        coords = np.argwhere(mask)

        if coords.size == 0:
            logger.error("No non-white content found in illustration")
            return False

        # Crop to content (argwhere returns [row, col] = [y, x])
        min_y, min_x = coords.min(axis=0)
        max_y, max_x = coords.max(axis=0)
        cropped = illustration.crop((min_x, min_y, max_x + 1, max_y + 1))

        # Scale to fixed size, preserving aspect ratio
        cropped.thumbnail(illustration_size, Image.Resampling.LANCZOS)

        # Center on the template at the configured coordinates
        paste_x = center_x - cropped.width // 2
        paste_y = center_y - cropped.height // 2
        template.paste(cropped, (paste_x, paste_y), cropped)

        # Save as JPEG
        template.convert("RGB").save(output_path, "JPEG", quality=95)
        return True

    except (OSError, ValueError) as exc:
        logger.error("Thumbnail compositing failed: %s", exc)
        return False


def render_thumbnail(
    prompt: str,
    folder: Path,
    api_key: str,
    template_path: Path,
    channel_config: ChannelConfig,
    image_model: str,
    thumbnail_settings: ThumbnailConfig,
) -> Path | None:
    """Generate a fresh illustration for prompt and composite it in folder."""
    folder.mkdir(parents=True, exist_ok=True)
    illustration = folder / "generated_thumbnail.png"
    thumbnail = folder / "thumbnail.jpg"
    if not generate_illustration(
        prompt,
        illustration,
        api_key,
        channel_config.illustration_prompt_template,
        image_model,
    ):
        return None
    if not composite_thumbnail(
        illustration,
        thumbnail,
        template_path,
        channel_config.illustration_center_x,
        channel_config.illustration_center_y,
        thumbnail_settings.illustration_size,
    ):
        return None
    (folder / ".thumbnail_prompt").write_text(prompt, encoding="utf-8")
    return thumbnail


def generate_thumbnails(
    clips: list[dict],
    work_dir: Path,
    api_key: str,
    template_path: Path,
    channel_config: ChannelConfig,
    image_model: str,
    thumbnail_settings: ThumbnailConfig,
) -> list[dict]:
    """Draw each clip's thumbnail in parallel, reusing any drawn from the same
    prompt before."""
    logger.info("[5/6] Generating thumbnails...")

    def draw(i: int) -> str | None:
        clip = clips[i]
        folder = work_dir / "clips" / clip_folder_name(i, clip["title"])
        prompt = thumbnail_prompt_text(clip)
        thumbnail = folder / "thumbnail.jpg"
        prompt_file = folder / ".thumbnail_prompt"
        if (
            thumbnail.exists()
            and prompt_file.exists()
            and prompt_file.read_text(encoding="utf-8").strip() == prompt
        ):
            return str(thumbnail)
        drawn = render_thumbnail(
            prompt,
            folder,
            api_key,
            template_path,
            channel_config,
            image_model,
            thumbnail_settings,
        )
        return str(drawn) if drawn else None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for i, path in enumerate(pool.map(draw, range(len(clips)))):
            clips[i]["thumbnail_path"] = path
    return clips
