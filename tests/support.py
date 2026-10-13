"""Boot the real MCP server over HTTP and drive it the way the scheduled agent
does, for two channels: "blow" (one speaker, a fixed thumbnail template) and
"science" (any speaker, headshot thumbnails).

Local providers replace external services; clients, persistence and media run normally.
"""

import asyncio
import base64
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

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


@pytest.fixture
def server(tmp_path):
    blow = tmp_path / "channels/blow"
    queue = [
        _clip(blow, "aaaaaaaaaaa", "01", status="uploaded"),
        _clip(blow, "aaaaaaaaaaa", "02", speaker="Jonathan Blow"),
        _clip(blow, "aaaaaaaaaaa", "03", speaker="Jonathan Blow"),
    ]
    (blow / "upload_queue.json").write_text(json.dumps(queue))
    science = tmp_path / "channels/science"
    (science / "upload_queue.json").parent.mkdir(parents=True)
    clip = _clip(science, "bbbbbbbbbbb", "01", speaker="Robert Sapolsky")
    (science / "upload_queue.json").write_text(json.dumps([clip]))
    (tmp_path / "config.toml").write_text(CONFIG)
    with (
        provider(FakeOpenRouter) as endpoint,
        app(
            tmp_path,
            launcher="from clipy.mcp_server import main; main()",
            MATRIX_ACCESS_TOKEN="",
            OPENROUTER_API_KEY="unused",
            OPENROUTER_API_URL=endpoint + "/",
            ASSEMBLYAI_API_KEY="unused",
        ) as (started, _),
    ):
        yield started


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


def wait_job(server, job, channel="blow"):
    """Observe a background job through the public MCP API, without 10s sleeps."""

    async def go():
        async with connect(server[0], channel) as client:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                jobs = data(await client.call_tool("list_jobs", {}))
                found = next(j for j in jobs if j["id"] == job["id"])
                if found["status"] in {"succeeded", "failed"}:
                    return found
                await asyncio.sleep(0.05)
            raise AssertionError(f"Job did not finish: {found}")

    return asyncio.run(go())


class YouTube(BaseHTTPRequestHandler):
    existing: ClassVar[list] = []
    sessions = 0
    probes = 0
    thumbnails = 0
    reset = False
    inserted: ClassVar[dict] = {}
    deleted: ClassVar[list] = []

    def reply(self, body, status=200, **headers):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.endswith("/channels"):
            self.reply(
                {
                    "items": [
                        {"contentDetails": {"relatedPlaylists": {"uploads": "uploads"}}}
                    ]
                }
            )
        elif path.endswith("/playlistItems"):
            self.reply(
                {
                    "items": [
                        {"contentDetails": {"videoId": v["id"]}} for v in self.existing
                    ]
                }
            )
        elif path.endswith("/videos"):
            ids = parse_qs(urlsplit(self.path).query).get("id", [""])[0].split(",")
            self.reply({"items": [v for v in self.existing if v["id"] in ids]})
        else:
            self.reply({}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if urlsplit(self.path).path.endswith("/videos"):
            type(self).inserted = json.loads(raw)
            type(self).sessions += 1
            self.reply(
                {}, Location=f"http://127.0.0.1:{self.server.server_port}/session"
            )
        elif "/thumbnails" in self.path:
            type(self).thumbnails += 1
            self.reply({"items": []})
        else:
            self.reply({}, 404)

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if urlsplit(self.path).path.endswith("/videos"):
            update = json.loads(body)
            for v in self.existing:
                if v["id"] == update["id"]:
                    for key in ("snippet", "status"):
                        if key in update:
                            if key == "status":
                                v[key].pop("publishAt", None)
                            v[key].update(update[key])
            self.reply(update)
        elif self.headers.get("Content-Range", "").startswith("bytes */"):
            type(self).probes += 1
            self.reply({}, 308)
        elif self.reset:
            type(self).reset = False
            assert body
            self.connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            self.connection.close()
            self.close_connection = True
        else:
            assert body
            self.existing.append(
                {
                    "id": "new-upload",
                    "snippet": {
                        **self.inserted["snippet"],
                        "publishedAt": datetime.now(UTC).isoformat(),
                    },
                    "status": {**self.inserted["status"], "uploadStatus": "processed"},
                    "contentDetails": {"duration": "PT1S"},
                    "processingDetails": {"processingStatus": "succeeded"},
                }
            )
            self.reply({"id": "new-upload"})

    def do_DELETE(self):
        video_id = parse_qs(urlsplit(self.path).query)["id"][0]
        type(self).deleted.append(video_id)
        type(self).existing = [v for v in self.existing if v["id"] != video_id]
        self.reply({})

    def log_message(self, *args):
        pass


LAUNCHER = """
import json, os
from googleapiclient import discovery
original = discovery._retrieve_discovery_doc
def document(*args, **kwargs):
    d = json.loads(original(*args, **kwargs))
    d['rootUrl'] = os.environ['FAKE_GOOGLE_URL'] + '/'
    return json.dumps(d)
discovery._retrieve_discovery_doc = document
from pathlib import Path
if os.environ.get('FAKE_MEDIA'):
    import clipy.download
    clipy.download._download = lambda *args: Path(os.environ['FAKE_MEDIA'])
from clipy.mcp_server import main
main()
"""


def write_token(channel_dir):
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / ".youtube_token.json").write_text(
        json.dumps(
            {
                "token": "fixture",
                "refresh_token": "unused",
                "client_id": "fixture",
                "client_secret": "fixture",
                "expiry": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "scopes": [
                    "https://www.googleapis.com/auth/" + s
                    for s in (
                        "youtube",
                        "youtube.upload",
                        "yt-analytics.readonly",
                        "yt-analytics-monetary.readonly",
                    )
                ],
            }
        )
    )


@contextmanager
def provider(handler):
    http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=http.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    try:
        yield f"http://127.0.0.1:{http.server_port}"
    finally:
        http.shutdown()
        http.server_close()


@contextmanager
def app(root, launcher=LAUNCHER, **settings):
    port = _free_port()
    env = {
        **os.environ,
        "CLIPY_CONFIG": str(root / "config.toml"),
        "CLIPY_DATA_DIR": str(root),
        "CLIPY_MCP_TOKEN": TOKEN,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        **settings,
    }
    log = root / "server.log"
    with log.open("w") as output:
        proc = subprocess.Popen(
            [sys.executable, "-c", launcher], env=env, stdout=output, stderr=output
        )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                if requests.get(base + "/healthz", timeout=0.5).ok:
                    break
            except requests.ConnectionError:
                pass
            assert proc.poll() is None and time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        yield (base, root), env
    finally:
        proc.terminate()
        proc.wait(timeout=5)
