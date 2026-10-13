"""Publish, reconcile a lost response and repair a broken release over MCP/HTTP."""

import json
import subprocess
from datetime import UTC, datetime, timedelta

from support import (
    CONFIG,
    YouTube,
    _clip,
    app,
    call,
    data,
    provider,
    wait_job,
    write_token,
)


def setup(root, status="pending"):
    channel = root / "channels/blow"
    item = _clip(
        channel,
        "aaaaaaaaaaa",
        "01",
        status=status,
        start_ms=1300,
        cut_encoding="full",
        publish_at=(datetime.now(UTC) + timedelta(days=2))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    )
    media = channel / "aaaaaaaaaaa/clips/01 - Clip 01/video.mp4"
    media.write_bytes(b"video" * 400)
    queue = channel / "upload_queue.json"
    queue.write_text(json.dumps([item]))
    write_token(channel)
    (root / "config.toml").write_text(CONFIG)
    YouTube.existing = []
    YouTube.deleted = []
    YouTube.sessions = YouTube.probes = YouTube.thumbnails = 0
    YouTube.reset = False
    return item, queue, media


def test_scheduled_upload_survives_reset_and_lost_confirmation(tmp_path):
    item, queue, media = setup(tmp_path)
    YouTube.reset = True
    with (
        provider(YouTube) as endpoint,
        app(tmp_path, FAKE_GOOGLE_URL=endpoint) as (server, _),
    ):
        job = data(
            call(
                server,
                "publish_clip",
                {"clip_id": "aaaaaaaaaaa:01", "publish_at": item["publish_at"]},
            )
        )
        assert wait_job(server, job)["status"] == "succeeded"
        assert YouTube.probes == 1 and YouTube.sessions == 1
        saved = json.loads(queue.read_text())[0]
        assert saved["youtube_id"] == "new-upload"
        assert saved["publish_at"] == item["publish_at"]
        assert YouTube.inserted["status"]["privacyStatus"] == "private"
        assert YouTube.thumbnails == 1
        assert "watch?v=aaaaaaaaaaa&t=1s" in YouTube.inserted["snippet"]["description"]
        # The provider succeeded but the local upload confirmation was lost.
        item["status"] = "failed"
        queue.write_text(json.dumps([item]))
        job = data(call(server, "publish_clip", {"clip_id": "aaaaaaaaaaa:01"}))
        assert wait_job(server, job)["status"] == "succeeded"
        assert YouTube.sessions == 1 and len(YouTube.existing) == 1
        assert json.loads(queue.read_text())[0]["youtube_id"] == "new-upload"
        assert not media.exists()


def test_repair_preserves_release_and_never_replaces_public_video(tmp_path):
    item, queue, media = setup(tmp_path, "uploaded")
    item["youtube_id"] = "broken-upload"
    queue.write_text(json.dumps([item]))
    asset = tmp_path / "source.mp4"
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
    original = {
        "id": "broken-upload",
        "snippet": {
            "title": item["title"],
            "categoryId": "28",
            "description": "Original: https://youtube.com/watch?v=aaaaaaaaaaa",
            "publishedAt": "2026-10-05T00:00:00Z",
        },
        "status": {
            "privacyStatus": "public",
            "uploadStatus": "uploaded",
            "publishAt": item["publish_at"],
            "embeddable": True,
            "publicStatsViewable": True,
        },
        "processingDetails": {"processingStatus": "processing"},
        "contentDetails": {"duration": "PT1S"},
    }
    YouTube.existing = [original]
    with (
        provider(YouTube) as endpoint,
        app(tmp_path, FAKE_GOOGLE_URL=endpoint, FAKE_MEDIA=str(asset)) as (server, _),
    ):
        refused = data(call(server, "repair_upload", {"video_id": "broken-upload"}))
        assert wait_job(server, refused)["status"] == "failed"
        assert YouTube.sessions == 0 and YouTube.deleted == [] and media.exists()
        original["status"]["privacyStatus"] = "private"
        media.unlink()
        job = data(call(server, "repair_upload", {"video_id": "broken-upload"}))
        assert wait_job(server, job)["status"] == "succeeded"
        assert YouTube.sessions == 1 and YouTube.deleted == ["broken-upload"]
        assert YouTube.inserted["status"] == {"privacyStatus": "private"}
        saved = json.loads(queue.read_text())[0]
        assert saved["youtube_id"] == "new-upload"
        assert datetime.fromisoformat(
            YouTube.existing[0]["status"]["publishAt"]
        ) == datetime.fromisoformat(item["publish_at"])
        resumed = data(call(server, "repair_upload", {"video_id": "broken-upload"}))
        assert wait_job(server, resumed)["status"] == "succeeded"
        assert YouTube.sessions == 1
