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
    (source / "transcript.json").write_text("{}")
    (source / "sentences.json").write_text("[]")
    (source / "clips.json").write_text(
        json.dumps(
            [
                {
                    "title": "Programming",
                    "description": "A programming discussion.",
                    "start_ms": 1300,
                    "end_ms": 6300,
                    "chapters": [],
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
        for _ in range(2):
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
