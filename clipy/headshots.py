"""Speaker headshots, the left panel of a thumbnail on channels without a fixed
template. The agent finds, crops and saves them itself (web search, then
image tools in its sandbox) to <data root>/headshots/<slug>.jpg, one per
speaker, shared by every channel.
"""

import re
from pathlib import Path

from PIL import Image, ImageOps

from .channel import data_root

PANEL = (720, 1080)


def path(speaker: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", speaker.lower()).strip("-")
    if not slug:
        raise ValueError("A speaker needs a name.")
    return data_root() / "headshots" / f"{slug}.jpg"


def panel(speaker: str) -> Image.Image:
    """The speaker's headshot filling the panel: the widest 2:3 portrait of
    it, centred across and kept near the top so the head stays in."""
    headshot = path(speaker)
    if not headshot.exists():
        raise ValueError(
            f"No headshot of {speaker}: find a photo, crop it to a 2:3 portrait "
            f"of the head and shoulders, and save it as {headshot}."
        )
    image = ImageOps.exif_transpose(Image.open(headshot)).convert("RGB")
    w, h = image.size
    if w * PANEL[1] > h * PANEL[0]:  # too wide: trim the sides
        width = h * PANEL[0] // PANEL[1]
        box = ((w - width) // 2, 0, (w + width) // 2, h)
    else:  # too tall: trim mostly from the bottom
        height = w * PANEL[1] // PANEL[0]
        top = (h - height) // 10
        box = (0, top, w, top + height)
    return image.crop(box).resize(PANEL, Image.Resampling.LANCZOS)
