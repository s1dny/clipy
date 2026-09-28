import json
import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)


def openrouter_request(body: dict, timeout: int) -> dict[str, Any]:
    """POST a chat completion to OpenRouter and return the parsed response."""
    resp = requests.post(
        os.environ.get("OPENROUTER_API_URL")
        or "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json=body,
        timeout=timeout,
    )
    if not resp.ok:
        logger.error("API error %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    data = resp.json()
    if error := data.get("error"):
        message = error.get("message") if isinstance(error, dict) else None
        raise RuntimeError(f"OpenRouter error: {message or error}")
    return data


def openrouter_message(data: dict) -> dict[str, Any]:
    """The first choice's message from a chat-completions response."""
    return data["choices"][0]["message"]


def openrouter_json(body: dict, timeout: int) -> dict:
    """A JSON-mode completion's object, ignoring any text around it."""
    text = openrouter_message(openrouter_request(body, timeout)).get("content")
    if not isinstance(text, str):
        raise TypeError("OpenRouter response did not include text content")
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


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


def load_or_compute(path: Path, compute_fn: Callable[..., Any], *args: Any) -> Any:
    """Load JSON from path if it exists, otherwise compute, save, and return."""
    if path.exists():
        return read_json_file(path)
    result = compute_fn(*args)
    write_json_file(path, result)
    return result
