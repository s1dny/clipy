import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

logger = logging.getLogger(__name__)


def openrouter_request(api_key: str, body: dict, timeout: int) -> dict[str, Any]:
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
        logger.error("API error %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    data = resp.json()
    error = data.get("error")
    if error:
        if isinstance(error, dict):
            message = error.get("message") or json.dumps(error)
        else:
            message = str(error)
        raise RuntimeError(f"OpenRouter error: {message}")
    return data


def openrouter_message(data: dict) -> dict[str, Any]:
    """The first choice's message from a chat-completions response."""
    return data["choices"][0]["message"]


def openrouter_message_text(data: dict) -> str:
    """Extract plain text content from an OpenRouter response."""
    message = openrouter_message(data)
    content = message.get("content")
    if isinstance(content, str):
        return content
    raise ValueError("OpenRouter response did not include text content")


def openrouter_message_images(data: dict) -> list[dict[str, Any]]:
    """Extract image items from an OpenRouter response."""
    message = openrouter_message(data)
    images = message.get("images")
    if isinstance(images, list):
        return [image for image in images if isinstance(image, dict)]
    return []


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
    logger.debug("Running: %s", cmd_str)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=False)
    return result.returncode, result.stdout, result.stderr


def read_json_file(path: Path) -> Any:
    """Load JSON from disk using UTF-8."""
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def write_json_file(path: Path, data: Any) -> None:
    """Write JSON to disk using UTF-8, creating the parent directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def load_or_compute(
    path: Path,
    compute_fn: Callable[..., Any],
    *args: Any,
    skip_message: str | None = None,
) -> Any:
    """Load JSON from path if it exists, otherwise compute, save, and return."""
    if path.exists():
        if skip_message:
            logger.info(skip_message)
        return read_json_file(path)
    result = compute_fn(*args)
    write_json_file(path, result)
    return result
