import json
import logging
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from config import OPENROUTER_API_URL

logger = logging.getLogger("clipper")


def openrouter_request(api_key: str, body: dict, timeout: int) -> dict:
    """Make a request to OpenRouter API and return the parsed response."""
    resp = requests.post(
        OPENROUTER_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    if not resp.ok:
        logger.error(f"API error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
    return resp.json()


def extract_video_id(url: str) -> str:
    """Extract the video ID from a YouTube URL."""
    parsed = urlparse(url)
    if parsed.hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        return parse_qs(parsed.query)["v"][0]
    if parsed.hostname in ("youtu.be",):
        return parsed.path.lstrip("/")
    raise ValueError(f"Cannot extract video ID from URL: {url}")


def run_cmd(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    cmd_str = " ".join(cmd)
    if len(cmd_str) > 64:
        cmd_str = cmd_str[:61] + "..."
    logger.debug(f"Running: {cmd_str}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return result.returncode, result.stdout, result.stderr


def load_or_compute(path: Path, compute_fn, *args, skip_message: str | None = None):
    """Load JSON from path if it exists, otherwise compute, save, and return."""
    if path.exists():
        if skip_message:
            logger.info(skip_message)
        with open(path) as f:
            return json.load(f)
    result = compute_fn(*args)
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    return result
