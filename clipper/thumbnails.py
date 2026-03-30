import base64
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from .clips import clip_folder_name
from .common import openrouter_message_images, openrouter_request
from .config import ChannelConfig

IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "black-forest-labs/flux.2-pro")
IMAGE_GENERATION_TIMEOUT = int(os.environ.get("IMAGE_GENERATION_TIMEOUT", "120"))
_illustration_size = os.environ.get("ILLUSTRATION_SIZE", "700,700")
ILLUSTRATION_SIZE = tuple(int(x) for x in _illustration_size.split(","))

logger = logging.getLogger(__name__)


def thumbnail_prompt_text(clip: dict) -> str:
    """Normalize the prompt used both for generation and cache invalidation."""
    return clip.get("thumbnail_prompt", clip["title"]).strip()


def generate_illustration(
    prompt: str, output_path: Path, api_key: str, prompt_template: str
) -> bool:
    """Generate an illustration to be composited onto thumbnail template."""
    logger.info("Generating illustration: %s...", output_path.name)

    try:
        illustration_prompt = prompt_template.format(prompt=prompt)
        data = openrouter_request(
            api_key,
            {
                "model": IMAGE_MODEL,
                "messages": [{"role": "user", "content": illustration_prompt}],
                "modalities": ["image"],
                "provider": {"sort": "price"},
            },
            IMAGE_GENERATION_TIMEOUT,
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

    except Exception as e:
        logger.error("Illustration generation failed: %s", e)
        return False


def composite_thumbnail(
    illustration_path: Path,
    output_path: Path,
    template_path: Path,
    center_x: int,
    center_y: int,
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
        cropped.thumbnail(ILLUSTRATION_SIZE, Image.Resampling.LANCZOS)

        # Center on the template at the configured coordinates
        paste_x = center_x - cropped.width // 2
        paste_y = center_y - cropped.height // 2
        template.paste(cropped, (paste_x, paste_y), cropped)

        # Save as JPEG
        template.convert("RGB").save(output_path, "JPEG", quality=95)
        return True

    except Exception as e:
        logger.error("Thumbnail compositing failed: %s", e)
        return False


def _generate_single_thumbnail(args: tuple) -> tuple[int, str | None]:
    """Generate a single thumbnail (for use with ThreadPoolExecutor)."""
    (
        i,
        clip,
        clips_dir,
        api_key,
        template_path,
        prompt_template,
        center_x,
        center_y,
    ) = args
    folder = clips_dir / clip_folder_name(i, clip["title"])
    folder.mkdir(exist_ok=True)
    thumb_path = folder / "thumbnail.jpg"
    generated_thumbnail_path = folder / "generated_thumbnail.png"

    if thumb_path.exists() and generated_thumbnail_path.exists():
        logger.info(
            "Skipping clip %s: %s (thumbnail exists)",
            i + 1,
            clip["title"],
            extra={"highlighter": None},
        )
        return i, str(thumb_path)

    try:
        if not generated_thumbnail_path.exists():
            if not generate_illustration(
                thumbnail_prompt_text(clip),
                generated_thumbnail_path,
                api_key,
                prompt_template,
            ):
                return i, None

        if composite_thumbnail(
            generated_thumbnail_path,
            thumb_path,
            template_path,
            center_x,
            center_y,
        ):
            return i, str(thumb_path)
        return i, None
    except Exception as e:
        logger.error("Thumbnail generation failed for clip %s: %s", i + 1, e)
        return i, None


def generate_thumbnails(
    clips: list[dict],
    work_dir: Path,
    api_key: str,
    template_path: Path,
    channel_config: ChannelConfig,
) -> list[dict]:
    """Generate thumbnails for each clip in parallel. Detects prompt changes and missing thumbnails."""
    logger.info("[5/6] Generating thumbnails...")

    clips_dir = work_dir / "clips"

    # Detect prompt changes and delete stale thumbnails so they get regenerated
    for i, clip in enumerate(clips):
        folder = clips_dir / clip_folder_name(i, clip["title"])
        if not folder.exists():
            continue
        prompt_file = folder / ".thumbnail_prompt"
        current_prompt = thumbnail_prompt_text(clip)

        if prompt_file.exists():
            if prompt_file.read_text(encoding="utf-8").strip() != current_prompt:
                logger.info(
                    "Prompt changed for clip %s: %s",
                    i + 1,
                    clip["title"],
                    extra={"highlighter": None},
                )
                for stale_asset in (
                    folder / "thumbnail.jpg",
                    folder / "generated_thumbnail.png",
                ):
                    if stale_asset.exists():
                        stale_asset.unlink()
        prompt_file.write_text(current_prompt, encoding="utf-8")

    tasks = [
        (
            i,
            clip,
            clips_dir,
            api_key,
            template_path,
            channel_config.illustration_prompt_template,
            channel_config.illustration_center_x,
            channel_config.illustration_center_y,
        )
        for i, clip in enumerate(clips)
    ]

    with ThreadPoolExecutor(max_workers=10) as pool:
        for i, thumb_path in pool.map(_generate_single_thumbnail, tasks):
            clips[i]["thumbnail_path"] = thumb_path

    return clips
