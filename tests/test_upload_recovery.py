"""Exercise upload recovery through MCP and Google's real HTTP client."""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import urlsplit

import pytest
import requests
from test_mcp_server import CONFIG, TOKEN, _clip, _free_port, call, data


class YouTube(BaseHTTPRequestHandler):
    existing: ClassVar[list] = []
    sessions = 0
    probes = 0
    thumbnails = 0
    reset = False

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
            self.reply({"items": self.existing})
        else:
            self.reply({}, 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if urlsplit(self.path).path.endswith("/videos"):
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
        if self.headers.get("Content-Range", "").startswith("bytes */"):
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
            self.reply({"id": "new-upload"})

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
from clipy.mcp_server import main
main()
"""


@pytest.mark.parametrize(
    "mode", ["reset", "missing", "existing", "ambiguous", "processing"]
)
def test_publish_recovers_without_duplicate_upload(tmp_path, mode):
    source = "aaaaaaaaaaa"
    item = _clip(
        tmp_path / "channels/blow",
        source,
        "01",
        status="pending" if mode == "reset" else "failed",
        publish_at="2026-11-01T11:00:00Z",
    )
    media = tmp_path / "channels/blow" / source / "clips/01 - Clip 01/video.mp4"
    media.write_bytes(b"video" * 400)
    queue = tmp_path / "channels/blow/upload_queue.json"
    queue.write_text(json.dumps([item]))
    token = tmp_path / "channels/blow/.youtube_token.json"
    token.write_text(
        json.dumps(
            {
                "token": "test",
                "refresh_token": "unused",
                "client_id": "test",
                "client_secret": "test",
                "expiry": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "scopes": [
                    "https://www.googleapis.com/auth/" + s
                    for s in [
                        "youtube",
                        "youtube.upload",
                        "yt-analytics.readonly",
                        "yt-analytics-monetary.readonly",
                    ]
                ],
            }
        )
    )
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    video = {
        "id": "existing-upload",
        "snippet": {
            "title": item["title"],
            "description": f"Original: https://youtube.com/watch?v={source}",
            "publishedAt": "2026-10-05T00:00:00Z",
        },
        "status": {
            "privacyStatus": "private",
            "uploadStatus": "processed",
            "publishAt": item["publish_at"],
        },
        "contentDetails": {"duration": "PT10M"},
    }
    YouTube.existing = (
        [video]
        if mode in {"existing", "processing"}
        else [video, {**video, "id": "other-upload"}]
        if mode == "ambiguous"
        else []
    )
    if mode == "processing":
        video["status"]["uploadStatus"] = "uploading"
    YouTube.sessions = YouTube.probes = YouTube.thumbnails = 0
    YouTube.reset = mode == "reset"
    fake = ThreadingHTTPServer(("127.0.0.1", 0), YouTube)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", LAUNCHER],
        env={
            **os.environ,
            "CLIPY_CONFIG": str(config),
            "CLIPY_DATA_DIR": str(tmp_path),
            "CLIPY_MCP_TOKEN": TOKEN,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "FAKE_GOOGLE_URL": f"http://127.0.0.1:{fake.server_port}",
        },
    )
    server = (f"http://127.0.0.1:{port}", tmp_path)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if requests.get(server[0] + "/healthz", timeout=1).ok:
                    break
            except requests.ConnectionError:
                pass
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        result = data(
            call(
                server,
                "publish_clip",
                {
                    "clip_id": source + ":01",
                    **({"publish_at": item["publish_at"]} if mode == "reset" else {}),
                },
            )
        )
        job = data(call(server, "wait_for_job", {"job_id": result["id"]}))
        saved = json.loads(queue.read_text())[0]
        if mode in {"ambiguous", "processing"}:
            assert job["status"] == "failed"
            assert (
                "Multiple YouTube uploads" if mode == "ambiguous" else "not complete"
            ) in job["error"]
            assert YouTube.sessions == YouTube.thumbnails == 0
            assert media.exists()
        else:
            assert job["status"] == "succeeded", job
            assert saved["status"] == "uploaded"
            assert saved["publish_at"] == item["publish_at"]
            assert YouTube.sessions == (1 if mode in {"reset", "missing"} else 0)
            assert YouTube.thumbnails == 1
            assert not media.exists()
            assert saved["youtube_id"] == (
                "new-upload" if mode in {"reset", "missing"} else "existing-upload"
            )
            if mode == "reset":
                assert YouTube.probes == 1
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        fake.shutdown()
        fake.server_close()
