"""Resume a cached source through the real CLI, ffmpeg, thumbnail API and queue."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from support import CONFIG, FakeOpenRouter, provider


def ffmpeg(*args):
    return subprocess.check_output(["ffmpeg", "-v", "error", "-y", *map(str, args)])


@pytest.mark.parametrize(
    "codec,extension", [("libx264", "mp4"), ("libvpx-vp9", "webm")]
)
def test_source_resume_exports_synchronized_media_without_duplicate_queue(
    tmp_path, codec, extension
):
    source = tmp_path / "channels/blow/aaaaaaaaaaa"
    source.mkdir(parents=True)
    reference = tmp_path / f"reference.{extension}"
    encoding = (
        ["-preset", "ultrafast"]
        if codec == "libx264"
        else ["-deadline", "realtime", "-cpu-used", "8"]
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=192x108:rate=30:duration=8",
        "-c:v",
        codec,
        *encoding,
        "-threads",
        "1",
        "-g",
        "150",
        reference,
    )
    audio = tmp_path / "reference-audio.webm"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=sin(2*PI*(300*t+25*t*t)):s=48000:d=8",
        "-c:a",
        "libopus",
        audio,
    )
    shutil.copy(reference, source / f"video.{extension}")
    shutil.copy(audio, source / "audio.webm")
    (tmp_path / "config.toml").write_text(CONFIG)
    utterances = [{"speaker": "A", "text": "A discussion.", "start": 0, "end": 8000}]
    (source / "transcript.json").write_text(json.dumps({"utterances": utterances}))
    selections_file = tmp_path / "selections.json"
    selections_file.write_text(
        json.dumps(
            [
                {
                    "title": "Programming",
                    "description": "A programming discussion.",
                    "start_ms": 1300,
                    "end_ms": 6300,
                    "chapters": [],
                    "speaker": "Jonathan Blow",
                    "thumbnail_prompt": "a binary",
                }
            ]
        )
    )
    with provider(FakeOpenRouter) as endpoint:
        env = {
            **os.environ,
            "CLIPY_CONFIG": str(tmp_path / "config.toml"),
            "CLIPY_DATA_DIR": str(tmp_path),
            "ASSEMBLYAI_API_KEY": "unused",
            "OPENROUTER_API_KEY": "unused",
            "OPENROUTER_API_URL": endpoint + "/",
        }
        for command in ("prepare", "transcript"):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from clipy.cli import main; main()",
                    command,
                    "blow",
                    "https://youtube.com/watch?v=aaaaaaaaaaa",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            if command == "transcript":
                assert json.loads(result.stdout)["sentences"] == utterances
        for attempt in range(2):
            # A restarted run still downloads audio; its finished cut and queue are reused.
            if not (source / "audio.webm").exists():
                shutil.copy(audio, source / "audio.webm")
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from clipy.cli import main; main()",
                    "process",
                    "blow",
                    "https://youtube.com/watch?v=aaaaaaaaaaa",
                    *(["--clips", str(selections_file)] if attempt == 0 else []),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0, result.stderr
    queue = json.loads((source.parent / "upload_queue.json").read_text())
    assert len(queue) == 1 and queue[0]["status"] == "pending"
    clip = Path(queue[0]["clip_path"])
    thumbnail = Image.open(queue[0]["thumbnail_path"])
    assert thumbnail.size == (1920, 1080)
    metadata = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                "stream=codec_type,codec_name,nb_read_frames:format=duration",
                "-of",
                "json",
                str(clip),
            ]
        )
    )
    streams = {s["codec_type"]: s for s in metadata["streams"]}
    assert (
        streams["video"]["codec_name"] == "h264"
        and streams["audio"]["codec_name"] == "aac"
    )
    assert float(metadata["format"]["duration"]) == pytest.approx(5, abs=0.05)
    assert int(streams["video"]["nb_read_frames"]) == pytest.approx(150, abs=2)
    for at in (0, 4):

        def frame(path, start):
            return np.frombuffer(
                ffmpeg(
                    "-ss",
                    start,
                    "-i",
                    path,
                    "-frames:v",
                    "1",
                    "-pix_fmt",
                    "rgb24",
                    "-f",
                    "rawvideo",
                    "-",
                ),
                dtype=np.uint8,
            ).astype(float)

        assert np.mean(np.abs(frame(clip, at) - frame(reference, 1.3 + at))) < 5

        def samples(path, start):
            return np.frombuffer(
                ffmpeg(
                    "-ss",
                    start,
                    "-i",
                    path,
                    "-t",
                    "0.5",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "8000",
                    "-f",
                    "f32le",
                    "-",
                ),
                dtype=np.float32,
            )

        actual, expected = samples(clip, at), samples(audio, 1.3 + at)
        length = min(len(actual), len(expected))
        # AAC/Opus decoding can shift a few samples. Match within 10ms, well
        # below a frame, so a wrong source range or drifting audio still fails.
        margin = 80
        assert (
            max(
                np.corrcoef(
                    actual[margin : length - margin],
                    expected[margin + shift : length - margin + shift],
                )[0, 1]
                for shift in range(-margin, margin + 1)
            )
            > 0.97
        )


def test_agent_reads_full_source_and_submits_renderable_selections(server):
    from support import call, data, wait_job

    _, root = server
    url = "https://youtube.com/watch?v=ccccccccccc"
    source = root / "channels/blow/ccccccccccc"
    source.mkdir()
    utterances = [
        {
            "speaker": speaker,
            "text": f"Discussion {i}.",
            "start": i * 160000,
            "end": (i + 1) * 160000,
        }
        for i, speaker in enumerate(("A", "B", "A", "A"))
    ]
    (source / "transcript.json").write_text(json.dumps({"utterances": utterances}))
    prepared = wait_job(server, data(call(server, "prepare_source", {"url": url})))
    assert prepared["status"] == "succeeded", prepared
    sentences = []
    offset = 0
    while offset is not None:
        page = data(
            call(
                server,
                "get_source_transcript",
                {"url": url, "offset": offset, "limit": 1},
            )
        )
        sentences.extend(page["sentences"])
        offset = page["next_offset"]
    assert sentences == utterances
    assert call(
        server, "get_source_transcript", {"url": url}, channel="science"
    ).is_error
    assert call(server, "process_source", {"url": url}).is_error
    assert call(
        server,
        "get_source_transcript",
        {"url": "https://youtube.com/watch?v=../../science/bbbbbbbbbbb"},
    ).is_error
    assert call(
        server, "process_source", {"url": url, "clips": [{"start_ms": 4, "end_ms": 2}]}
    ).is_error
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:size=640x360:rate=1:duration=640",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        source / "video.mp4",
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo:d=640",
        "-c:a",
        "libopus",
        source / "audio.webm",
    )
    selections = [
        {
            "title": "An editorial choice",
            "description": "An accurate argument summary.",
            "start_ms": sentences[1]["start"],
            "end_ms": sentences[-1]["end"],
            "thumbnail_prompt": "a bold object",
            "chapters": [],
            "source": {
                "name": "Fixture",
                "streamed_on": "2020-01-02",
                "evidence": "Fixture metadata",
            },
        }
    ]
    # Public agent contract rejects short final cuts, missing dates and forbidden punctuation.
    assert call(
        server,
        "process_source",
        {"url": url, "clips": [selections[0] | {"end_ms": 161000}]},
    ).is_error
    assert call(
        server,
        "process_source",
        {"url": url, "clips": [selections[0] | {"source": None}]},
    ).is_error
    assert call(
        server,
        "process_source",
        {
            "url": url,
            "clips": [selections[0] | {"description": "Forbidden \u2014 punctuation."}],
        },
    ).is_error
    before = data(call(server, "list_queue"))
    args = {"url": url, "clips": selections}
    failed = wait_job(
        server,
        data(
            call(
                server,
                "process_source",
                args | {"clips": [selections[0] | {"end_ms": 800000}]},
            )
        ),
    )
    assert failed["status"] == "failed"
    assert not (source / "clips.json").exists()
    rendered = wait_job(server, data(call(server, "process_source", args)))
    assert rendered["status"] == "succeeded", rendered
    queue = data(call(server, "list_queue"))
    added = [clip for clip in queue if clip["id"] not in {old["id"] for old in before}]
    assert [(clip["title"], clip["description"]) for clip in added] == [
        (clip["title"], clip["description"]) for clip in selections
    ]
    clip_path = Path(
        json.loads((source.parent / "upload_queue.json").read_text())[-1]["clip_path"]
    )
    duration = float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(clip_path),
            ]
        )
    )
    assert duration == pytest.approx(
        (selections[0]["end_ms"] - selections[0]["start_ms"]) / 1000, abs=0.05
    )
    # A retry reuses the saved editorial choices and export, without duplication.
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo:d=640",
        "-c:a",
        "libopus",
        source / "audio.webm",
    )
    resumed = wait_job(server, data(call(server, "process_source", {"url": url})))
    assert resumed["status"] == "succeeded", resumed
    assert data(call(server, "list_queue")) == queue
    changed = wait_job(
        server,
        data(
            call(
                server,
                "process_source",
                args | {"clips": [selections[0] | {"start_ms": 0}]},
            )
        ),
    )
    assert changed["status"] == "failed"
    assert data(call(server, "list_queue")) == queue

    # Re-edit a pending export through the public tool, preserving its full eight minutes.
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:size=640x360:rate=1:duration=640",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        source / "video.mp4",
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo:d=640",
        "-c:a",
        "libopus",
        source / "audio.webm",
    )
    attribution = selections[0]["source"] | {"name": "Fixture revised"}
    edit = data(
        call(server, "edit_video", {"clip_id": "ccccccccccc:01", "source": attribution})
    )
    assert wait_job(server, edit)["status"] == "succeeded"
    report = data(call(server, "get_edit_report", {"clip_id": "ccccccccccc:01"}))
    assert report["report"]["duration_ms"] == 480000
    assert report["report"]["source"]["name"] == "Fixture revised"
    assert "Source:" not in attribution["name"]

    # A source can explicitly produce nothing, without downloading media or
    # requiring a headshot, and that decision survives a retry.
    empty_url = "https://youtube.com/watch?v=ddddddddddd"
    empty_source = root / "channels/science/ddddddddddd"
    empty_source.mkdir()
    (empty_source / "sentences.json").write_text(json.dumps(sentences))
    science_before = data(call(server, "list_queue", channel="science"))
    for selected in ({"clips": []}, {}):
        job = data(
            call(
                server,
                "process_source",
                {
                    "url": empty_url,
                    "speaker": "Robert Sapolsky",
                    **selected,
                },
                channel="science",
            )
        )
        assert wait_job(server, job, channel="science")["status"] == "succeeded"
    assert json.loads((empty_source / "clips.json").read_text()) == []
    assert data(call(server, "list_queue", channel="science")) == science_before
    assert not (empty_source / "clips").exists()
