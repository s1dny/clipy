"""Boot the real MCP server over HTTP and drive it the way the scheduled agent
does, for two channels: "blow" (one speaker, a fixed thumbnail template) and
"science" (any speaker, headshot thumbnails).

YouTube and AssemblyAI are never called; OpenRouter is a local fake that
draws a blue disc, so thumbnails are really drawn and composed.
"""

import asyncio
import base64
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2
import pytest
import requests
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from PIL import Image, ImageDraw

TOKEN = "s" * 40
ROOT = Path(__file__).resolve().parent.parent
BLUE = (40, 120, 220)
HEADSHOT_RED = (200, 30, 30)

CONFIG = """
[models]
clip = "test/clip"
image = "test/image"
speech = "test"

[defaults]
description_template = "{clip_description}\\n\\nOriginal: {original_url}"
clip_extraction_prompt_template = "Clips of {speaker} as JSON."
illustration_prompt_template = "Draw {prompt} on white."

[channels.blow]
handle = "@JonathanBlowClips"
speaker = "Jonathan Blow"
thumbnail_template = "blow.jpg"

[channels.science]
handle = "@ScienceClipsDaily"
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeOpenRouter(BaseHTTPRequestHandler):
    """Answers every image request with a blue disc on white."""

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        image = Image.new("RGB", (1024, 1024), "white")
        ImageDraw.Draw(image).ellipse((200, 200, 824, 824), fill=BLUE)
        png = io.BytesIO()
        image.save(png, "PNG")
        url = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode()
        body = json.dumps(
            {"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _clip(channel_dir: Path, source: str, number: str, **fields) -> dict:
    folder = channel_dir / source / "clips" / f"{number} - Clip {number}"
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"")
    (folder / "thumbnail.jpg").write_bytes(
        (ROOT / "thumbnails" / "blow.jpg").read_bytes()
    )
    return {
        "title": f"Clip {number}",
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
    blow = data / "channels" / "blow"
    queue = [
        _clip(
            blow,
            "aaaaaaaaaaa",
            "01",
            status="uploaded",
            uploaded_at=datetime.now(UTC).isoformat(),
        ),
        _clip(blow, "aaaaaaaaaaa", "02", speaker="Jonathan Blow"),
        _clip(blow, "aaaaaaaaaaa", "03", speaker="Jonathan Blow"),
    ]
    (blow / "upload_queue.json").write_text(json.dumps(queue))
    science = data / "channels" / "science"
    queue = [_clip(science, "bbbbbbbbbbb", "01", speaker="Robert Sapolsky")]
    (science / "upload_queue.json").write_text(json.dumps(queue))
    config = data / "config.toml"
    config.write_text(CONFIG)

    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenRouter)
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    port = _free_port()
    env = {
        **os.environ,
        "CLIPY_DATA_DIR": str(data),
        "CLIPY_CONFIG": str(config),
        "CLIPY_MCP_TOKEN": TOKEN,
        "MATRIX_ACCESS_TOKEN": "",
        "OPENROUTER_API_KEY": "unused",
        "OPENROUTER_API_URL": f"http://127.0.0.1:{fake.server_port}/",
        "ASSEMBLYAI_API_KEY": "unused",
        "PORT": str(port),
        "HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "clipy.mcp_server"], cwd=data, env=env
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
    yield base, data
    proc.terminate()
    proc.wait(timeout=10)
    fake.shutdown()


def connect(base: str, channel: str) -> Client:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    return Client(
        streamable_http_client(
            f"{base}/mcp/{channel}",
            http_client=httpx2.AsyncClient(headers=headers, timeout=60),
        )
    )


def call(server, tool: str, args: dict | None = None, channel: str = "blow"):
    async def go():
        async with connect(server[0], channel) as client:
            return await client.call_tool(tool, args or {})

    return asyncio.run(go())


def text(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


def data(result):
    structured = result.structured_content
    if structured is not None and set(structured) == {"result"}:
        return structured["result"]
    return structured if structured is not None else json.loads(text(result))


def image(result) -> Image.Image:
    block = next(b for b in result.content if b.type == "image")
    return Image.open(io.BytesIO(base64.b64decode(block.data))).convert("RGB")


def near(pixel, color, tolerance=30) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, color, strict=True))


def test_endpoints_need_the_token_and_a_channel(server):
    base, _ = server
    assert requests.post(f"{base}/mcp/blow", timeout=5).status_code == 401
    wrong = {"Authorization": "Bearer wrong"}
    assert (
        requests.post(f"{base}/mcp/blow", headers=wrong, timeout=5).status_code == 401
    )
    right = {"Authorization": f"Bearer {TOKEN}"}
    for path in ("/mcp", "/mcp/nope"):
        assert requests.post(base + path, headers=right, timeout=5).status_code == 404

    async def names():
        async with connect(base, "science") as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    assert {
        "channel_report",
        "process_source",
        "publish_clip",
        "write_notes",
        "sample_source",
        "wait_for_job",
    } <= asyncio.run(names())


def test_each_channel_keeps_its_own_notes_and_queue(server):
    _, root = server
    call(server, "write_notes", {"content": "Tested titles with 'Jai'."})
    call(server, "write_notes", {"content": "Sapolsky wins."}, channel="science")
    assert "Tested titles with 'Jai'." in text(call(server, "read_notes"))
    science_notes = text(call(server, "read_notes", channel="science"))
    assert "Sapolsky wins." in science_notes
    assert "Jai" not in science_notes
    assert "Sapolsky" in (root / "channels" / "science" / "notes.md").read_text()

    science = data(call(server, "list_queue", channel="science"))
    assert [(c["id"], c["speaker"]) for c in science] == [
        ("bbbbbbbbbbb:01", "Robert Sapolsky")
    ]


def test_pending_clips_can_be_edited_and_discarded(server):
    pending = data(call(server, "list_queue"))
    assert [c["id"] for c in pending] == ["aaaaaaaaaaa:02", "aaaaaaaaaaa:03"]

    edited = data(
        call(
            server,
            "edit_clip",
            {"clip_id": "aaaaaaaaaaa:02", "title": "Jonathan Blow: Rust Won't Win"},
        )
    )
    assert edited["title"] == "Jonathan Blow: Rust Won't Win"

    call(server, "discard_clip", {"clip_id": "aaaaaaaaaaa:03", "reason": "off topic"})
    assert [c["id"] for c in data(call(server, "list_queue"))] == ["aaaaaaaaaaa:02"]


def test_template_thumbnails_keep_the_channel_layout(server):
    call(
        server,
        "edit_clip",
        {"clip_id": "aaaaaaaaaaa:02", "thumbnail_prompt": "a bloated binary"},
    )
    thumbnail = image(call(server, "view_thumbnail", {"clip_id": "aaaaaaaaaaa:02"}))
    assert thumbnail.size == (1920, 1080)
    template = Image.open(ROOT / "thumbnails" / "blow.jpg").convert("RGB")
    assert near(thumbnail.getpixel((300, 300)), template.getpixel((300, 300)))
    # The illustration is centred in the white area right of the photo.
    assert near(thumbnail.getpixel((1274, 540)), BLUE)
    assert near(thumbnail.getpixel((700, 540)), (255, 255, 255))


def test_headshot_channels_need_the_speakers_headshot(server):
    _, root = server
    url = "https://www.youtube.com/watch?v=ccccccccccc"
    refused = call(
        server,
        "process_source",
        {"url": url, "speaker": "Robert Sapolsky"},
        channel="science",
    )
    assert refused.is_error
    headshot = root / "headshots" / "robert-sapolsky.jpg"
    assert str(headshot) in text(refused)
    unnamed = call(server, "process_source", {"url": url}, channel="science")
    assert unnamed.is_error
    assert "main speaker" in text(unnamed)

    # The agent saves a headshot itself; a landscape one is cropped to fit.
    headshot.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1600, 900), HEADSHOT_RED).save(headshot)
    call(
        server,
        "edit_clip",
        {"clip_id": "bbbbbbbbbbb:01", "thumbnail_prompt": "a neuron firing"},
        channel="science",
    )
    thumbnail = image(
        call(server, "view_thumbnail", {"clip_id": "bbbbbbbbbbb:01"}, "science")
    )
    assert thumbnail.size == (1920, 1080)
    assert near(thumbnail.getpixel((360, 540)), HEADSHOT_RED)
    assert near(thumbnail.getpixel((900, 100)), (255, 255, 255))
    assert near(thumbnail.getpixel((1370, 540)), BLUE)


def test_a_clip_cannot_be_published_twice(server):
    result = call(server, "publish_clip", {"clip_id": "aaaaaaaaaaa:01"})
    assert result.is_error
    assert "Clip is uploaded, not pending" in text(result)
    assert data(call(server, "list_jobs")) == []


def test_prompt_overrides_are_validated_and_per_channel(server):
    bad = call(server, "update_prompts", {"description_template": "no placeholders"})
    assert bad.is_error

    call(
        server,
        "update_prompts",
        {"clip_extraction_prompt_template": "Prefer programming rants. JSON only."},
    )
    prompts = data(call(server, "get_prompts"))
    assert prompts["clip_extraction_prompt_template"].startswith("Prefer programming")
    assert prompts["illustration_prompt_template"] == "Draw {prompt} on white."
    science = data(call(server, "get_prompts", channel="science"))
    assert science["clip_extraction_prompt_template"] == "Clips of {speaker} as JSON."


def test_waiting_on_an_unknown_job_says_so(server):
    result = call(server, "wait_for_job", {"job_id": "nope", "max_minutes": 1})
    assert result.is_error
    assert "No job 'nope'" in text(result)


def test_report_needs_matrix(server):
    result = call(server, "send_report", {"message": "## Today\n\n- nothing"})
    assert result.is_error
    assert "Matrix is not configured" in text(result)
