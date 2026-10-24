"""Execute the agent's source ranges, context cards and inspected frame holds.

All editorial choices are supplied by the agent. No model, automatic silence
threshold, or topic heuristic chooses edits here. Reports use output timestamps.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .clips import ClipSelection
from .common import read_json_file, write_json_file
from .cutting import AAC, X264, _duration, _ffprobe, _run


def has_edits(clip: dict) -> bool:
    return any(clip.get(k) for k in ("keep", "annotations", "visual_holds"))


def media_info(path: Path, ffmpeg: str) -> dict:
    result = subprocess.run(
        [
            _ffprobe(ffmpeg),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def frame(path: Path, at_ms: int, ffmpeg: str) -> bytes:
    if at_ms < 0:
        raise ValueError("Frame timestamp must be nonnegative.")
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            str(at_ms / 1000),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    if not result.stdout:
        raise ValueError("No frame at that timestamp.")
    return result.stdout


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        os.environ.get("CLIPY_FONT", ""),
        f"/usr/share/fonts/truetype/dejavu/{name}",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError(
        "Install fonts-dejavu-core or set CLIPY_FONT to a TrueType font."
    )


def _card(card, width: int, height: int, path: Path):
    """A restrained lower third, never an invented illustration."""
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    size = max(10, round(height * 0.028))
    font, label_font = _font(size), _font(max(9, round(size * 0.72)), True)
    margin = max(8, round(width * 0.025))
    pad = max(8, round(height * 0.019))
    max_width = min(width - 2 * margin, round(width * 0.77))
    lines = []
    for paragraph in card.text.splitlines():
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=font) > max_width - 2 * pad:
                if not line:
                    raise ValueError("Annotation contains a word too wide to display.")
                lines.append(line)
                line = word
            else:
                line = trial
        if line:
            lines.append(line)
    if not lines or len(lines) > 5:
        raise ValueError("Annotation must fit in five readable lines.")
    line_height = round(size * 1.35)
    box_height = pad * 3 + size + len(lines) * line_height
    if box_height > height * 0.35:
        raise ValueError("Annotation would obscure too much of the source.")
    box_width = min(
        max_width,
        max(round(draw.textlength(line, font=font)) for line in lines) + 2 * pad,
    )
    box_width = max(
        box_width, round(draw.textlength(card.label.upper(), font=label_font)) + 2 * pad
    )
    left = margin if card.position.endswith("left") else width - margin - box_width
    top = (
        round(height * 0.15)
        if card.position.startswith("top")
        else height - margin - box_height
    )
    draw.rounded_rectangle(
        (left, top, left + box_width, top + box_height),
        radius=pad,
        fill=(14, 19, 26, 235),
    )
    draw.text(
        (left + pad, top + pad),
        card.label.upper(),
        font=label_font,
        fill=(126, 204, 222),
    )
    for i, line in enumerate(lines):
        draw.text(
            (left + pad, top + pad * 2 + size + i * line_height),
            line,
            font=font,
            fill=(248, 249, 250),
        )
    image.save(path)


def edit_report(clip: dict) -> dict:
    selection = ClipSelection.model_validate(clip)
    ranges = selection.keep or [
        {
            "start_ms": selection.start_ms,
            "end_ms": selection.end_ms,
            "reason": "Original excerpt",
        }
    ]
    ranges = [r.model_dump() if hasattr(r, "model_dump") else r for r in ranges]
    timeline, cuts = [], []
    output = 0
    previous = selection.start_ms
    for span in ranges:
        if previous < span["start_ms"]:
            cuts.append(
                {
                    "output_ms": output,
                    "removed_source_start_ms": previous,
                    "removed_source_end_ms": span["start_ms"],
                    "reason": span["reason"],
                }
            )
        duration = span["end_ms"] - span["start_ms"]
        timeline.append(
            {"output_start_ms": output, "output_end_ms": output + duration, **span}
        )
        output += duration
        previous = span["end_ms"]
    if previous < selection.end_ms:
        cuts.append(
            {
                "output_ms": output,
                "removed_source_start_ms": previous,
                "removed_source_end_ms": selection.end_ms,
                "reason": "End at the selected conclusion",
            }
        )
    return {
        "title": selection.title,
        "duration_ms": output,
        "timeline": timeline,
        "cuts": cuts,
        "annotations": [a.model_dump() for a in selection.annotations],
        "visual_holds": [h.model_dump() for h in selection.visual_holds],
    }


def render_edit(
    video: Path, audio: Path, clip: dict, output: Path, ffmpeg: str
) -> None:
    selection = ClipSelection.model_validate(clip)
    report = edit_report(clip)
    streams = media_info(video, ffmpeg)["streams"]
    visual = next(s for s in streams if s["codec_type"] == "video")
    width, height = visual["width"], visual["height"]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as work:
        work = Path(work)
        # Seek once to the bounding excerpt; all trims below are relative to it.
        seek = [
            "-ss",
            str(selection.start_ms / 1000),
            "-t",
            str((selection.end_ms - selection.start_ms) / 1000),
        ]
        command = [
            ffmpeg,
            "-v",
            "error",
            "-y",
            *seek,
            "-i",
            str(video),
            *seek,
            "-i",
            str(audio),
        ]
        filters = []
        spans = report["timeline"]
        count = len(spans)
        if count > 1:
            filters += [
                f"[0:v]split={count}" + "".join(f"[vs{i}]" for i in range(count)),
                f"[1:a]asplit={count}" + "".join(f"[as{i}]" for i in range(count)),
            ]
        for i, span in enumerate(spans):
            start = (span["start_ms"] - selection.start_ms) / 1000
            end = (span["end_ms"] - selection.start_ms) / 1000
            v, a = (f"vs{i}", f"as{i}") if count > 1 else ("0:v", "1:a")
            filters += [
                f"[{v}]trim=start={start}:end={end},setpts=PTS-STARTPTS,fps=30,setsar=1[v{i}]",
                f"[{a}]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a{i}]",
            ]
        filters.append(
            "".join(f"[v{i}][a{i}]" for i in range(count))
            + f"concat=n={count}:v=1:a=1[base][sound]"
        )
        current, index = "base", 2
        # Full-screen frame holds preserve the speaker's EXISTING drawing.
        for i, hold in enumerate(selection.visual_holds):
            still = work / f"hold-{i}.jpg"
            still.write_bytes(frame(video, hold.source_ms, ffmpeg))
            command += ["-loop", "1", "-framerate", "30", "-i", str(still)]
            target = f"hold{i}"
            filters.append(
                f"[{current}][{index}:v]overlay=0:0:enable='gte(t,{hold.start_ms / 1000})*lt(t,{hold.end_ms / 1000})'[{target}]"
            )
            current, index = target, index + 1
        for i, card in enumerate(selection.annotations):
            path = work / f"card-{i}.png"
            _card(card, width, height, path)
            command += ["-loop", "1", "-framerate", "30", "-i", str(path)]
            target = f"card{i}"
            filters.append(
                f"[{current}][{index}:v]overlay=0:0:enable='gte(t,{card.start_ms / 1000})*lt(t,{card.end_ms / 1000})'[{target}]"
            )
            current, index = target, index + 1
        partial = output.with_name(output.stem + ".partial.mp4")
        command += [
            "-filter_complex",
            ";\n".join(filters),
            "-filter_complex_threads",
            "1",
            "-map",
            f"[{current}]",
            "-map",
            "[sound]",
            *X264,
            *AAC,
            "-t",
            str(selection.duration_ms / 1000),
            "-movflags",
            "+faststart",
            str(partial),
        ]
        try:
            _run(command)
            metadata = media_info(partial, ffmpeg)
            if not {"audio", "video"} <= {s["codec_type"] for s in metadata["streams"]}:
                raise RuntimeError("Edited output is missing audio or video.")
            actual = _duration(_ffprobe(ffmpeg), partial)
            if abs(actual - selection.duration_ms / 1000) > 0.2:
                raise RuntimeError(
                    f"Edited duration mismatch: {actual}s vs {selection.duration_ms / 1000}s."
                )
            report["verification"] = {
                "duration_seconds": actual,
                "audio_and_video": True,
            }
            partial.replace(output)
            write_json_file(output.parent / "edit-report.json", report)
        finally:
            partial.unlink(missing_ok=True)


def report_for(path: Path) -> dict | None:
    report = path.parent / "edit-report.json"
    return read_json_file(report) if report.exists() else None
