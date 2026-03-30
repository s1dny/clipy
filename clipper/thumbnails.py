import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from .clips import clip_folder_name
from .common import openrouter_request

IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "black-forest-labs/flux.2-pro")
IMAGE_GENERATION_TIMEOUT = int(os.environ.get("IMAGE_GENERATION_TIMEOUT", "120"))
_illustration_size = os.environ.get("ILLUSTRATION_SIZE", "700,700")
ILLUSTRATION_SIZE = tuple(int(x) for x in _illustration_size.split(","))
ILLUSTRATION_CENTER_X = int(os.environ.get("ILLUSTRATION_CENTER_X", "850"))
ILLUSTRATION_CENTER_Y = int(os.environ.get("ILLUSTRATION_CENTER_Y", "350"))

logger = logging.getLogger("clipper")


def thumbnail_prompt_text(clip: dict) -> str:
    """Normalize the prompt used both for generation and cache invalidation."""
    return clip.get("thumbnail_prompt", clip["title"]).strip()


def generate_illustration(
    prompt: str, output_path: Path, api_key: str, prompt_template: str
) -> bool:
    """Generate an illustration to be composited onto thumbnail template."""
    logger.info(f"Generating illustration: {output_path.name}...")

    try:
        illustration_prompt = prompt_template.format(prompt=prompt)
        data = openrouter_request(
            api_key,
            {
                "model": IMAGE_MODEL,
                "messages": [{"role": "user", "content": illustration_prompt}],
                "provider": {"sort": "price"},
            },
            IMAGE_GENERATION_TIMEOUT,
        )

        message = data["choices"][0]["message"]

        # Check for images array (Gemini format)
        images = message.get("images", [])
        if images:
            img_data_url = images[0].get("image_url", {}).get("url", "")
            if img_data_url.startswith("data:image"):
                # Extract base64 data after the comma
                b64_data = img_data_url.split(",", 1)[1]
                img_bytes = base64.b64decode(b64_data)
                with open(output_path, "wb") as f:
                    f.write(img_bytes)
                return True

        logger.error("No image in response")
        return False

    except Exception as e:
        logger.error(f"Illustration generation failed: {e}")
        return False


def composite_thumbnail(
    illustration_path: Path, output_path: Path, template_path: Path
) -> bool:
    """Crop illustration to non-white content, scale, and center on template."""
    logger.info(f"Compositing thumbnail: {output_path.name}...")

    if not template_path.exists():
        logger.error(f"Thumbnail template not found at {template_path}")
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
        paste_x = ILLUSTRATION_CENTER_X - cropped.width // 2
        paste_y = ILLUSTRATION_CENTER_Y - cropped.height // 2
        template.paste(cropped, (paste_x, paste_y), cropped)

        # Save as JPEG
        template.convert("RGB").save(output_path, "JPEG", quality=95)
        return True

    except Exception as e:
        logger.error(f"Thumbnail compositing failed: {e}")
        return False


def _generate_single_thumbnail(args: tuple) -> tuple[int, str | None]:
    """Generate a single thumbnail (for use with ThreadPoolExecutor)."""
    i, clip, clips_dir, api_key, template_path, prompt_template = args
    folder = clips_dir / clip_folder_name(i, clip["title"])
    folder.mkdir(exist_ok=True)
    thumb_path = folder / "thumbnail.jpg"

    if thumb_path.exists():
        logger.info(f"Skipping clip {i + 1}: {clip['title']} (thumbnail exists)")
        return i, str(thumb_path)

    illustration_path = folder / "_temp_illustration.png"
    try:
        if generate_illustration(
            thumbnail_prompt_text(clip),
            illustration_path,
            api_key,
            prompt_template,
        ):
            if composite_thumbnail(illustration_path, thumb_path, template_path):
                return i, str(thumb_path)
        return i, None
    finally:
        if illustration_path.exists():
            illustration_path.unlink()


def generate_thumbnails(
    clips: list[dict],
    work_dir: Path,
    api_key: str,
    template_path: Path,
    prompt_template: str,
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
                logger.info(f"Prompt changed for clip {i + 1}: {clip['title']}")
                thumb = folder / "thumbnail.jpg"
                if thumb.exists():
                    thumb.unlink()
        prompt_file.write_text(current_prompt, encoding="utf-8")

    tasks = [
        (i, clip, clips_dir, api_key, template_path, prompt_template)
        for i, clip in enumerate(clips)
    ]

    with ThreadPoolExecutor(max_workers=10) as pool:
        for i, thumb_path in pool.map(_generate_single_thumbnail, tasks):
            clips[i]["thumbnail_path"] = thumb_path

    return clips
