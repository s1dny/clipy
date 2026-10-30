//! Execute the agent's source ranges, context cards and inspected frame
//! holds with one continuous ffmpeg encode. No model, silence threshold or
//! topic heuristic chooses edits here. Reports use output timestamps.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use tokio::process::Command;

use crate::cards;
use crate::clips::ClipSelection;

const X264: [&str; 8] = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
];
const AAC: [&str; 4] = ["-c:a", "aac", "-b:a", "192k"];

pub fn ffprobe(ffmpeg: &str) -> PathBuf {
    Path::new(ffmpeg).with_file_name("ffprobe")
}

async fn run(command: &mut Command) -> Result<Vec<u8>> {
    let output = command
        .stdin(std::process::Stdio::null())
        .output()
        .await
        .context("could not start ffmpeg")?;
    if !output.status.success() {
        bail!(
            "ffmpeg failed: {}",
            crate::app::tail(&String::from_utf8_lossy(&output.stderr), 500)
        );
    }
    Ok(output.stdout)
}

/// ffprobe's streams and format, as JSON.
pub async fn media_info(path: &Path, ffmpeg: &str) -> Result<Value> {
    let out = run(Command::new(ffprobe(ffmpeg))
        .args([
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
        ])
        .arg(path))
    .await?;
    Ok(serde_json::from_slice(&out)?)
}

pub async fn duration_seconds(path: &Path, ffmpeg: &str) -> f64 {
    let info = media_info(path, ffmpeg).await.unwrap_or_default();
    info["format"]["duration"]
        .as_str()
        .and_then(|d| d.parse().ok())
        .unwrap_or(0.0)
}

/// One JPEG frame at a timestamp.
pub async fn frame(path: &Path, at_ms: i64, ffmpeg: &str) -> Result<Vec<u8>> {
    if at_ms < 0 {
        bail!("Frame timestamp must be nonnegative.");
    }
    let out = run(Command::new(ffmpeg)
        .args(["-v", "error", "-ss", &seconds(at_ms), "-i"])
        .arg(path)
        .args([
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-",
        ]))
    .await?;
    if out.is_empty() {
        bail!("No frame at that timestamp.");
    }
    Ok(out)
}

fn seconds(ms: i64) -> String {
    format!("{}", ms as f64 / 1000.0)
}

/// Output timeline, removed ranges and the agent's cards, holds and source.
pub fn edit_report(selection: &ClipSelection) -> Value {
    let ranges: Vec<(i64, i64, String)> = if selection.keep.is_empty() {
        vec![(
            selection.start_ms,
            selection.end_ms,
            "Original excerpt".into(),
        )]
    } else {
        selection
            .keep
            .iter()
            .map(|r| (r.start_ms, r.end_ms, r.reason.clone()))
            .collect()
    };
    let (mut timeline, mut cuts) = (Vec::new(), Vec::new());
    let mut output = 0;
    let mut previous = selection.start_ms;
    for (start, end, reason) in ranges {
        if previous < start {
            cuts.push(json!({
                "output_ms": output,
                "removed_source_start_ms": previous,
                "removed_source_end_ms": start,
                "reason": reason,
            }));
        }
        timeline.push(json!({
            "output_start_ms": output,
            "output_end_ms": output + end - start,
            "start_ms": start,
            "end_ms": end,
            "reason": reason,
        }));
        output += end - start;
        previous = end;
    }
    if previous < selection.end_ms {
        cuts.push(json!({
            "output_ms": output,
            "removed_source_start_ms": previous,
            "removed_source_end_ms": selection.end_ms,
            "reason": "End at the selected conclusion",
        }));
    }
    json!({
        "title": selection.title,
        "duration_ms": output,
        "timeline": timeline,
        "cuts": cuts,
        "annotations": selection.annotations,
        "visual_holds": selection.visual_holds,
        "source": selection.source,
    })
}

/// Render the selection into `output` and return its verified edit report.
/// Card images are kept under `assets` so retries and recovery reuse them.
pub async fn render(
    video: &Path,
    audio: &Path,
    selection: &ClipSelection,
    output: &Path,
    assets: &Path,
    http: &reqwest::Client,
    ffmpeg: &str,
) -> Result<Value> {
    let mut report = edit_report(selection);
    let info = media_info(video, ffmpeg).await?;
    let stream = info["streams"]
        .as_array()
        .and_then(|s| s.iter().find(|s| s["codec_type"] == "video"))
        .context("The source has no video stream.")?;
    let width = stream["width"].as_u64().context("video width")? as u32;
    let height = stream["height"].as_u64().context("video height")? as u32;
    let rate = parse_rate(stream["avg_frame_rate"].as_str().unwrap_or("30/1"));
    let total = seconds(selection.duration_ms());
    let parent = output.parent().context("output has no folder")?;
    tokio::fs::create_dir_all(parent).await?;
    let work = tempfile_dir(parent)?;

    // Seek once to the bounding excerpt; every trim below is relative to it.
    let seek = [
        "-ss".to_owned(),
        seconds(selection.start_ms),
        "-t".to_owned(),
        seconds(selection.end_ms - selection.start_ms),
    ];
    let mut command = Command::new(ffmpeg);
    command
        .args(["-v", "error", "-y"])
        .args(&seek)
        .arg("-i")
        .arg(video);
    command.args(&seek).arg("-i").arg(audio);
    let mut filters = Vec::new();
    let spans = report["timeline"].as_array().cloned().unwrap_or_default();
    let count = spans.len();
    if count > 1 {
        filters.push(format!(
            "[0:v]split={count}{}",
            (0..count).map(|i| format!("[vs{i}]")).collect::<String>()
        ));
        filters.push(format!(
            "[1:a]asplit={count}{}",
            (0..count).map(|i| format!("[as{i}]")).collect::<String>()
        ));
    }
    for (i, span) in spans.iter().enumerate() {
        let start = seconds(span["start_ms"].as_i64().unwrap_or(0) - selection.start_ms);
        let end = seconds(span["end_ms"].as_i64().unwrap_or(0) - selection.start_ms);
        let (v, a) = if count > 1 {
            (format!("vs{i}"), format!("as{i}"))
        } else {
            ("0:v".into(), "1:a".into())
        };
        filters.push(format!(
            "[{v}]trim=start={start}:end={end},setpts=PTS-STARTPTS,fps={rate},setsar=1[v{i}]"
        ));
        filters.push(format!(
            "[{a}]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a{i}]"
        ));
    }
    filters.push(format!(
        "{}concat=n={count}:v=1:a=1[base][sound]",
        (0..count)
            .map(|i| format!("[v{i}][a{i}]"))
            .collect::<String>()
    ));
    let mut current = "base".to_owned();
    let mut index = 2;
    let mut overlay =
        |command: &mut Command, image: &Path, enable: Option<(i64, i64)>, target: String| {
            command
                .args([
                    "-loop",
                    "1",
                    "-framerate",
                    &rate.to_string(),
                    "-t",
                    &total,
                    "-i",
                ])
                .arg(image);
            let enable = enable
                .map(|(s, e)| format!(":enable='gte(t,{})*lt(t,{})'", seconds(s), seconds(e)))
                .unwrap_or_default();
            filters.push(format!(
                "[{current}][{index}:v]overlay=0:0{enable}[{target}]"
            ));
            current = target;
            index += 1;
        };
    // Full-screen frame holds preserve the speaker's EXISTING drawing.
    for (i, hold) in selection.visual_holds.iter().enumerate() {
        let still = work.path().join(format!("hold-{i}.jpg"));
        tokio::fs::write(&still, frame(video, hold.source_ms, ffmpeg).await?).await?;
        overlay(
            &mut command,
            &still,
            Some((hold.start_ms, hold.end_ms)),
            format!("hold{i}"),
        );
    }
    for (i, card) in selection.annotations.iter().enumerate() {
        let path = work.path().join(format!("card-{i}.png"));
        let art = match &card.image_url {
            Some(url) => Some(cards::image_asset(http, url, assets).await?),
            None => None,
        };
        let window = Some((card.start_ms, card.end_ms));
        let (card, target) = (card.clone(), path.clone());
        tokio::task::spawn_blocking(move || cards::card(&card, art, width, height, &target))
            .await??;
        overlay(&mut command, &path, window, format!("card{i}"));
    }
    if let Some(source) = &selection.source {
        let path = work.path().join("source.png");
        let (source, target) = (source.clone(), path.clone());
        tokio::task::spawn_blocking(move || cards::source_bug(&source, width, height, &target))
            .await??;
        overlay(&mut command, &path, None, "attributed".into());
    }
    let partial = output.with_extension("partial.mp4");
    command
        .arg("-filter_complex")
        .arg(filters.join(";\n"))
        .args([
            "-filter_complex_threads",
            "1",
            "-map",
            &format!("[{current}]"),
            "-map",
            "[sound]",
        ])
        .args(X264)
        .args(AAC)
        .args(["-t", &total, "-movflags", "+faststart"])
        .arg(&partial);
    let result = async {
        run(&mut command).await?;
        let verification = verify(&partial, selection.duration_ms(), ffmpeg).await?;
        tokio::fs::rename(&partial, output).await?;
        Ok::<_, anyhow::Error>(verification)
    }
    .await;
    let _ = tokio::fs::remove_file(&partial).await;
    report["verification"] = result?;
    Ok(report)
}

/// Both streams present and the expected length, within 0.2 seconds.
pub async fn verify(path: &Path, duration_ms: i64, ffmpeg: &str) -> Result<Value> {
    let metadata = media_info(path, ffmpeg).await?;
    let kinds: Vec<&str> = metadata["streams"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|s| s["codec_type"].as_str())
        .collect();
    if !(kinds.contains(&"audio") && kinds.contains(&"video")) {
        bail!("Edited output is missing audio or video.");
    }
    let actual = duration_seconds(path, ffmpeg).await;
    let expected = duration_ms as f64 / 1000.0;
    if (actual - expected).abs() > 0.2 {
        bail!("Edited duration mismatch: {actual}s vs {expected}s.");
    }
    Ok(json!({ "duration_seconds": actual, "audio_and_video": true }))
}

fn parse_rate(rate: &str) -> f64 {
    let value = match rate.split_once('/') {
        Some((n, d)) => {
            let (n, d): (f64, f64) = (n.parse().unwrap_or(0.0), d.parse().unwrap_or(0.0));
            if d == 0.0 { 0.0 } else { n / d }
        }
        None => rate.parse().unwrap_or(0.0),
    };
    if value > 0.0 { value } else { 30.0 }
}

fn tempfile_dir(parent: &Path) -> Result<TempDir> {
    let path = parent.join(format!(".render-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&path)?;
    Ok(TempDir(path))
}

struct TempDir(PathBuf);

impl TempDir {
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
