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
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from test_mcp_server import CONFIG, TOKEN, _clip, _free_port, call, data


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


@pytest.mark.parametrize(
    "mode",
    [
        "reset",
        "legacy",
        "missing",
        "existing",
        "ambiguous",
        "processing",
        "repair",
        "public",
        "rejected",
        "working",
        "fresh",
    ],
)
def test_publish_recovers_without_duplicate_upload(tmp_path, mode):
    repairing = mode in {"repair", "public", "rejected", "working", "fresh"}
    source = "aaaaaaaaaaa"
    item = _clip(
        tmp_path / "channels/blow",
        source,
        "01",
        status="pending" if mode == "reset" else "failed",
        start_ms=7300,
        cut_encoding="full",
        publish_at=(datetime.now(UTC) + timedelta(days=2))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    )
    media = tmp_path / "channels/blow" / source / "clips/01 - Clip 01/video.mp4"
    media.write_bytes(b"video" * 400)
    queue = tmp_path / "channels/blow/upload_queue.json"
    if mode == "legacy":
        item.pop("cut_encoding")
        item.pop("start_ms")
    if repairing:
        item.update(status="uploaded", youtube_id="existing-upload")
    queue.write_text(json.dumps([item]))
    asset = tmp_path / "source.mp4"
    if mode in {"repair", "legacy"}:
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x120:rate=24:duration=1",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=1",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(asset),
            ],
            check=True,
        )
        (media.parents[2] / "clips.json").write_text(
            json.dumps([{"start_ms": 0, "end_ms": 1000}])
        )
        if mode == "repair":
            media.unlink()
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
            "categoryId": "28",
            "description": f"Original: https://youtube.com/watch?v={source}",
            "publishedAt": "2026-10-05T00:00:00Z",
        },
        "status": {
            "privacyStatus": "private",
            "uploadStatus": "processed",
            "embeddable": True,
            "publicStatsViewable": True,
            "publishAt": item["publish_at"],
        },
        "contentDetails": {"duration": "PT10M"},
    }
    YouTube.existing = (
        [video]
        if mode in {"existing", "processing"} or repairing
        else [video, {**video, "id": "other-upload"}]
        if mode == "ambiguous"
        else []
    )
    if mode == "processing":
        video["status"]["uploadStatus"] = "uploading"
    if repairing:
        video["status"]["uploadStatus"] = (
            "uploaded" if mode != "working" else "processed"
        )
        video["processingDetails"] = {
            "processingStatus": "processing" if mode != "working" else "succeeded"
        }
        if mode == "public":
            video["status"]["privacyStatus"] = "public"
        if mode == "rejected":
            video["status"]["uploadStatus"] = "rejected"
        if mode == "fresh":
            video["snippet"]["publishedAt"] = datetime.now(UTC).isoformat()
    YouTube.deleted = []
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
            "FAKE_MEDIA": str(asset) if mode in {"repair", "legacy"} else "",
        },
    )
    server = (f"http://127.0.0.1:{port}", tmp_path)
    try:
        deadline = time.monotonic() + 60
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
                "repair_upload" if repairing else "publish_clip",
                {
                    **(
                        {"video_id": "existing-upload"}
                        if repairing
                        else {"clip_id": source + ":01"}
                    ),
                    **({"publish_at": item["publish_at"]} if mode == "reset" else {}),
                },
            )
        )
        job = data(call(server, "wait_for_job", {"job_id": result["id"]}))
        saved = json.loads(queue.read_text())[0]
        if repairing:
            if mode == "repair":
                assert job["status"] == "succeeded", job
                assert saved["youtube_id"] == "new-upload"
                assert saved["repair"]["completed_at"]
                assert YouTube.deleted == ["existing-upload"]
                assert YouTube.sessions == 1
                assert YouTube.inserted["status"] == {"privacyStatus": "private"}
                assert YouTube.existing[0]["status"]["embeddable"] is True
                assert YouTube.existing[0]["status"]["publicStatsViewable"] is True
                assert not media.exists()
                assert datetime.fromisoformat(
                    YouTube.existing[0]["status"]["publishAt"]
                ) == datetime.fromisoformat(item["publish_at"])
                resumed = data(
                    call(server, "repair_upload", {"video_id": "existing-upload"})
                )
                assert (
                    data(call(server, "wait_for_job", {"job_id": resumed["id"]}))[
                        "status"
                    ]
                    == "succeeded"
                )
                assert YouTube.sessions == 1
            else:
                assert job["status"] == "failed", job
                assert YouTube.sessions == 0 and YouTube.deleted == []
                assert media.exists()
        elif mode in {"ambiguous", "processing"}:
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
            assert YouTube.sessions == (
                1 if mode in {"reset", "missing", "legacy"} else 0
            )
            assert YouTube.thumbnails == 1
            assert media.exists() == (mode != "existing")
            assert saved["youtube_id"] == (
                "new-upload"
                if mode in {"reset", "missing", "legacy"}
                else "existing-upload"
            )
            if mode == "legacy":
                assert saved["cut_encoding"] == "full"
                assert (
                    "watch?v=aaaaaaaaaaa&t=0s"
                    in YouTube.inserted["snippet"]["description"]
                )
                assert media.with_suffix(".encoding").read_text() == "full"
            if mode in {"reset", "missing"}:
                assert (
                    "watch?v=aaaaaaaaaaa&t=7s"
                    in YouTube.inserted["snippet"]["description"]
                )
                assert saved["follow_comment_pending"]
            if mode == "reset":
                assert YouTube.probes == 1
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        fake.shutdown()
        fake.server_close()
