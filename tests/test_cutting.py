"""Cut clips with real ffmpeg from sources shaped like YouTube's: separate
video and audio streams, keyframes 5 seconds apart."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipy.cutting import cut_clip

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")


def ffmpeg(*args: str) -> str:
    return subprocess.run(
        [FFMPEG, "-v", "error", "-y", *args], capture_output=True, text=True, check=True
    ).stderr


def probe(path: Path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-count_frames", "-show_entries",
         "stream=codec_type,codec_name,nb_read_frames:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return json.loads(out)


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("media")
    frames = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=60:duration=30"]
    ffmpeg(*frames, "-c:v", "libx264", "-g", "300", "-keyint_min", "300",
           "-sc_threshold", "0", "-pix_fmt", "yuv420p", str(tmp / "h264.mp4"))  # fmt: skip
    ffmpeg(*frames, "-c:v", "libvpx-vp9", "-deadline", "realtime", "-g", "300",
           str(tmp / "vp9.webm"))  # fmt: skip
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=30",
           "-c:a", "libopus", str(tmp / "audio.webm"))  # fmt: skip
    return tmp


def first_frame_matches_source(clip: Path, source: Path, at: float) -> float:
    """SSIM of the clip's first frame against the source frame at `at`."""
    graph = (
        "[0:v]trim=end_frame=1,setpts=PTS-STARTPTS[a];"
        "[1:v]trim=end_frame=1,setpts=PTS-STARTPTS[b];[a][b]ssim=stats_file=-"
    )
    stats = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(clip), "-ss", str(at), "-i", str(source),
         "-lavfi", graph, "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return float(stats.split("All:")[1].split()[0])


@pytest.mark.parametrize("source", ["h264.mp4", "vp9.webm"])
def test_clip_is_frame_accurate_and_complete(sources, source):
    video = sources / source
    clip = sources / f"clip-{source}.mp4"

    assert cut_clip(video, sources / "audio.webm", 7_300, 20_000, clip, FFMPEG)

    info = probe(clip)
    streams = {s["codec_type"]: s for s in info["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"
    assert float(info["format"]["duration"]) == pytest.approx(12.7, abs=0.05)
    assert int(streams["video"]["nb_read_frames"]) == pytest.approx(12.7 * 60, abs=2)
    assert ffmpeg("-i", str(clip), "-f", "null", "-") == ""  # decodes cleanly
    assert first_frame_matches_source(clip, video, 7.3) > 0.98
    assert not list(sources.glob("*.partial.*"))


def test_failed_cut_leaves_nothing(sources, tmp_path):
    clip = tmp_path / "video.mp4"
    assert not cut_clip(
        tmp_path / "missing.mp4", sources / "audio.webm", 0, 5_000, clip, FFMPEG
    )
    assert not clip.exists()
    assert not list(tmp_path.glob("*.partial.*"))
