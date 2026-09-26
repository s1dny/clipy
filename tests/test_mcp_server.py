"""Boot the real MCP server over HTTP and drive it the way the scheduled agent does.

Only paths that stay off the network are exercised: YouTube, OpenRouter and
AssemblyAI are never called.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests
from mcp import Client

SECRET = "s" * 40
NOT_TODAY = (datetime.now(ZoneInfo("Australia/Sydney")) + timedelta(days=3)).strftime(
    "%A"
)
ROOT = Path(__file__).resolve().parent.parent

CONFIG = """
[models]
clip = "test/clip"
image = "test/image"
speech = "test"

[defaults]
thumbnail_template = "blow.jpg"
description_template = "{clip_description}\\n\\nOriginal: {original_url}"
clip_extraction_prompt_template = "Return clips as JSON."
illustration_prompt_template = "Draw {prompt} on white."
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _clip(work_root: Path, source: str, number: str, **fields) -> dict:
    folder = work_root / source / "clips" / f"{number} - Clip {number}"
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"")
    (folder / "thumbnail.jpg").write_bytes(
        (ROOT / "thumbnails" / "blow.jpg").read_bytes()
    )
    return {
        "title": f"Jonathan Blow on Clip {number}",
        "description": "A clip.",
        "clip_path": str(folder / "video.mp4"),
        "thumbnail_path": str(folder / "thumbnail.jpg"),
        "duration_seconds": 600,
        "status": "pending",
        **fields,
    }


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    work_root = data / "channels" / "blow"
    queue = [
        _clip(
            work_root,
            "aaaaaaaaaaa",
            "01",
            status="uploaded",
            uploaded_at=datetime.now(UTC).isoformat(),
        ),
        _clip(work_root, "aaaaaaaaaaa", "02"),
        _clip(work_root, "aaaaaaaaaaa", "03"),
    ]
    (work_root / "upload_queue.json").write_text(json.dumps(queue))
    config = data / "config.toml"
    config.write_text(CONFIG)

    port = _free_port()
    env = {
        **os.environ,
        "CLIPPER_DATA_DIR": str(data),
        "CLIPPER_CONFIG": str(config),
        "CLIPPER_MCP_SECRET": SECRET,
        "OPENROUTER_API_KEY": "unused",
        "ASSEMBLYAI_API_KEY": "unused",
        "PORT": str(port),
        "HOST": "127.0.0.1",
        # Any day but today, so uploads are out of the batch window.
        "CLIPPER_BATCH_WEEKDAY": NOT_TODAY,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "clipper.mcp_server"],
        cwd=data,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            if requests.get(f"{base}/healthz", timeout=1).ok:
                break
        except requests.ConnectionError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            raise RuntimeError("MCP server did not start")
        time.sleep(0.2)
    yield base, work_root
    proc.terminate()
    proc.wait(timeout=10)


def call(base: str, tool: str, args: dict | None = None):
    async def go():
        async with Client(f"{base}/mcp/{SECRET}") as client:
            return await client.call_tool(tool, args or {})

    return asyncio.run(go())


def text(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


def data(result):
    structured = result.structured_content
    if structured is not None and set(structured) == {"result"}:
        return structured["result"]
    return structured if structured is not None else json.loads(text(result))


def test_endpoint_requires_the_secret_path(server):
    base, _ = server
    assert requests.post(f"{base}/mcp/wrong", timeout=5).status_code == 404

    async def names():
        async with Client(f"{base}/mcp/{SECRET}") as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    assert {
        "channel_report",
        "process_source",
        "publish_clip",
        "write_notes",
        "sample_source",
        "wait_for_job",
    } <= (asyncio.run(names()))


def test_notes_persist_between_runs(server):
    base, work_root = server
    call(base, "write_notes", {"content": "Tested titles with 'Jai'."})
    assert "Tested titles with 'Jai'." in text(call(base, "read_notes"))
    assert "Tested titles" in (work_root / "notes.md").read_text()


def test_pending_clips_can_be_edited_and_discarded(server):
    base, _ = server
    pending = data(call(base, "list_queue"))
    assert [c["id"] for c in pending] == ["aaaaaaaaaaa:02", "aaaaaaaaaaa:03"]

    edited = data(
        call(
            base,
            "edit_clip",
            {"clip_id": "aaaaaaaaaaa:02", "title": "Jonathan Blow: Rust Won't Win"},
        )
    )
    assert edited["title"] == "Jonathan Blow: Rust Won't Win"

    call(base, "discard_clip", {"clip_id": "aaaaaaaaaaa:03", "reason": "off topic"})
    ids = [c["id"] for c in data(call(base, "list_queue"))]
    assert ids == ["aaaaaaaaaaa:02"]

    thumbnail = call(base, "view_thumbnail", {"clip_id": "aaaaaaaaaaa:02"})
    assert thumbnail.content[0].type == "image"


def test_uploads_only_happen_in_the_weekly_batch(server):
    base, _ = server
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = call(
        base, "publish_clip", {"clip_id": "aaaaaaaaaaa:02", "publish_at": tomorrow}
    )
    assert result.is_error
    assert f"only in the weekly batch on {NOT_TODAY}" in text(result)
    assert data(call(base, "list_jobs")) == []


def test_uploads_must_be_scheduled(server):
    base, _ = server
    result = call(base, "publish_clip", {"clip_id": "aaaaaaaaaaa:02"})
    assert result.is_error
    assert data(call(base, "list_jobs")) == []


def test_prompt_overrides_are_validated_and_applied(server):
    base, _ = server
    bad = call(base, "update_prompts", {"description_template": "no placeholders"})
    assert bad.is_error

    call(
        base,
        "update_prompts",
        {"clip_extraction_prompt_template": "Prefer programming rants. JSON only."},
    )
    prompts = data(call(base, "get_prompts"))
    assert prompts["clip_extraction_prompt_template"].startswith("Prefer programming")
    assert prompts["illustration_prompt_template"] == "Draw {prompt} on white."


def test_waiting_on_an_unknown_job_says_so(server):
    base, _ = server
    result = call(base, "wait_for_job", {"job_id": "nope", "max_minutes": 1})
    assert result.is_error
    assert "No job 'nope'" in text(result)
