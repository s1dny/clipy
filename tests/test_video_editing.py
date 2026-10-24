"""Render an edited source through the real CLI, queue and FFmpeg, then resume."""

import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest
from support import CONFIG, FakeOpenRouter, provider


def test_cli_edited_ranges_context_hold_and_resume(tmp_path):
    source = tmp_path / "channels/blow/aaaaaaaaaaa"
    source.mkdir(parents=True)
    video = tmp_path / "reference.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=400:duration=6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
    )
    shutil.copy(video, source / "video.mp4")
    shutil.copy(video, source / "audio.mp4")
    (source / "sentences.json").write_text(
        json.dumps([{"start": 0, "end": 6000, "speaker": "A", "text": "A discussion."}])
    )
    (tmp_path / "config.toml").write_text(CONFIG)
    plan = {
        "title": "An edited discussion",
        "description": "An explanation.",
        "thumbnail_prompt": "a compiler",
        "start_ms": 500,
        "end_ms": 5500,
        "chapters": [],
        "keep": [
            {"start_ms": 500, "end_ms": 2000, "reason": "Opening argument"},
            {"start_ms": 3500, "end_ms": 5500, "reason": "Remove stream housekeeping"},
        ],
        "annotations": [
            {
                "start_ms": 0,
                "end_ms": 1000,
                "label": "Context",
                "text": "A compiler discussion.",
                "evidence": "Source transcript",
            }
        ],
        "visual_holds": [
            {
                "start_ms": 2000,
                "end_ms": 2500,
                "source_ms": 1000,
                "reason": "Preserve the completed original drawing",
            }
        ],
    }
    selections = tmp_path / "selections.json"
    selections.write_text(json.dumps([plan]))
    with provider(FakeOpenRouter) as endpoint:
        env = {
            **os.environ,
            "CLIPY_CONFIG": str(tmp_path / "config.toml"),
            "CLIPY_DATA_DIR": str(tmp_path),
            "OPENROUTER_API_URL": endpoint + "/",
            "OPENROUTER_API_KEY": "unused",
        }
        for attempt in range(2):
            shutil.copy(video, source / "audio.mp4")
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from clipy.cli import main; main()",
                    "process",
                    "blow",
                    "https://youtube.com/watch?v=aaaaaaaaaaa",
                    *(["--clips", str(selections)] if attempt == 0 else []),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            assert result.returncode == 0, result.stderr
    queue = json.loads((source.parent / "upload_queue.json").read_text())
    assert len(queue) == 1 and queue[0]["edited"] and queue[0]["status"] == "pending"
    assert queue[0]["duration_seconds"] == 4
    from pathlib import Path

    clip = Path(queue[0]["clip_path"])
    report = json.loads((clip.parent / "edit-report.json").read_text())
    assert report["duration_ms"] == 3500
    assert report["verification"]["duration_seconds"] == pytest.approx(3.5, abs=0.06)
    assert report["cuts"] == [
        {
            "output_ms": 1500,
            "removed_source_start_ms": 2000,
            "removed_source_end_ms": 3500,
            "reason": "Remove stream housekeeping",
        }
    ]
    assert report["annotations"][0]["evidence"] == "Source transcript"

    def pixels(path, at):
        raw = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(at),
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-",
            ]
        )
        return np.frombuffer(raw, dtype=np.uint8).reshape(360, 640, 3).astype(float)

    # Verify the content after the cut and the requested held frame, not just duration.
    assert np.abs(pixels(clip, 2.75) - pixels(video, 4.75)).mean() < 8
    assert np.abs(pixels(clip, 2.2) - pixels(video, 1)).mean() < 8
    original, edited = pixels(video, 1), pixels(clip, 0.5)
    assert np.abs(original[:180] - edited[:180]).mean() < 8
    assert np.abs(original[315:340, 20:140] - edited[315:340, 20:140]).mean() > 15

    # The held rollout must reject this real queued export before contacting YouTube.
    denied = subprocess.run(
        [
            sys.executable,
            "-c",
            'import json,sys; from clipy.upload import upload_video; upload_video(None,json.load(open(sys.argv[1]))[0],"{clip_description}","28")',
            str(source.parent / "upload_queue.json"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert denied.returncode != 0 and "held until the owner approves" in denied.stderr
