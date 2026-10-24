"""Publish, reconcile a lost response and repair a broken release over MCP/HTTP."""

import json
import shutil
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
        publish_at=(datetime.now(UTC) + timedelta(days=2))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    )
    media = channel / "aaaaaaaaaaa/clips/01 - Clip 01/video.mp4"
    from clipy.editing import render_edit

    source = {
        "name": "Fixture stream",
        "streamed_on": "2020-01-02",
        "evidence": "Fixture broadcast metadata",
    }
    item["source"] = source
    item["edited"] = True
    raw = root / "raw.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=320x180:rate=1/10:duration=480",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            "480",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            "-c:a",
            "pcm_s16le",
            str(raw),
        ],
        check=True,
    )
    render_edit(
        raw,
        raw,
        {
            "title": item["title"],
            "description": item["description"],
            "thumbnail_prompt": "a compiler",
            "start_ms": 0,
            "end_ms": 480000,
            "source": source,
        },
        media,
        "ffmpeg",
    )
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
        app(
            tmp_path, FAKE_GOOGLE_URL=endpoint, CLIPY_EDITED_PUBLISHING_ENABLED="1"
        ) as (server, _),
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
        assert (
            "Source: Fixture stream https://youtube.com/watch?v=aaaaaaaaaaa&t=1s"
            in YouTube.inserted["snippet"]["description"]
        )
        assert "Streamed: 2020-01-02" in YouTube.inserted["snippet"]["description"]
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
    asset = tmp_path / "source.mkv"
    shutil.copyfile(tmp_path / "raw.mkv", asset)
    (media.parents[2] / "clips.json").write_text(
        json.dumps(
            [
                {
                    "title": item["title"],
                    "description": item["description"],
                    "thumbnail_prompt": "a compiler",
                    "start_ms": 0,
                    "end_ms": 480000,
                    "source": item["source"],
                }
            ]
        )
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
        app(
            tmp_path,
            FAKE_GOOGLE_URL=endpoint,
            FAKE_MEDIA=str(asset),
            CLIPY_EDITED_PUBLISHING_ENABLED="1",
        ) as (server, _),
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
