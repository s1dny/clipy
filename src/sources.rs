//! Finding, vetting and downloading source videos with yt-dlp.
//!
//! Audio (for transcription) and video (for rendering) are separate YouTube
//! streams, fetched separately; nothing is merged or re-encoded here.

use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Duration;

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use tokio::process::Command;

use crate::app::{App, watch_url};
use crate::jobs::JobLog;

const AUDIO_FORMAT: &str = "bestaudio";
const VIDEO_FORMAT: &str = "bv*[vcodec^=avc1][height<=1080]/bv*[height<=1080]/bv*";
const ATTEMPTS: u64 = 4;

async fn yt_dlp(args: &[&str]) -> Result<Vec<u8>> {
    let output = Command::new("yt-dlp")
        .args(["--no-warnings", "--no-progress"])
        .args(args)
        .stdin(std::process::Stdio::null())
        .output()
        .await
        .context("could not start yt-dlp")?;
    if !output.status.success() {
        bail!(
            "yt-dlp failed: {}",
            crate::app::tail(&String::from_utf8_lossy(&output.stderr), 800)
        );
    }
    Ok(output.stdout)
}

async fn info(url: &str) -> Result<Value> {
    Ok(serde_json::from_slice(
        &yt_dlp(&["-J", "--skip-download", url]).await?,
    )?)
}

/// The finished audio.* or video.* file in `dir`, if there is one. yt-dlp
/// renames a download into place only once it is complete.
pub fn downloaded(dir: &Path, stem: &str) -> Option<PathBuf> {
    let mut found: Vec<PathBuf> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            let name = p.file_name().unwrap_or_default().to_string_lossy();
            name.starts_with(&format!("{stem}."))
                && !name.contains(".part")
                && !name.ends_with(".ytdl")
                && p.is_file()
        })
        .collect();
    found.sort();
    found.into_iter().next()
}

async fn download(
    url: &str,
    dir: &Path,
    stem: &str,
    format: &str,
    log: &JobLog,
) -> Result<PathBuf> {
    // Sources are shared, and preparation and rendering run in separate
    // lanes, so two jobs may want the same file at once: one downloads it.
    static DOWNLOADS: LazyLock<Mutex<HashMap<PathBuf, Arc<tokio::sync::Mutex<()>>>>> =
        LazyLock::new(Default::default);
    let lock = DOWNLOADS
        .lock()
        .unwrap()
        .entry(dir.join(stem))
        .or_default()
        .clone();
    let _guard = lock.lock().await;
    if let Some(existing) = downloaded(dir, stem) {
        log.info(format!("Reusing downloaded {stem}"));
        return Ok(existing);
    }
    tokio::fs::create_dir_all(dir).await?;
    let template = dir.join(format!("{stem}.%(ext)s"));
    let template = template.to_string_lossy();
    log.info(format!("Downloading {stem}"));
    for attempt in 1..=ATTEMPTS {
        // googlevideo stalls now and then on long downloads; retry and resume
        // from the partial file rather than failing the whole job.
        let result = yt_dlp(&[
            "-f",
            format,
            "-o",
            &template,
            "--socket-timeout",
            "60",
            "--retries",
            "30",
            "--fragment-retries",
            "30",
            "--print",
            "after_move:filepath",
            url,
        ])
        .await;
        match result {
            Ok(stdout) => {
                let path = String::from_utf8_lossy(&stdout)
                    .trim()
                    .lines()
                    .last()
                    .unwrap_or("")
                    .to_owned();
                let path = PathBuf::from(path);
                if !path.is_file() {
                    bail!("yt-dlp reported no downloaded {stem} file");
                }
                log.info(format!(
                    "Downloaded {stem}: {}",
                    path.file_name().unwrap_or_default().to_string_lossy()
                ));
                return Ok(path);
            }
            Err(error) if attempt < ATTEMPTS => {
                log.info(format!("{stem} download failed ({error}); resuming"));
                tokio::time::sleep(Duration::from_secs(30 * attempt)).await;
            }
            Err(error) => return Err(error),
        }
    }
    unreachable!()
}

pub async fn download_audio(app: &App, video_id: &str, log: &JobLog) -> Result<PathBuf> {
    download(
        &watch_url(video_id),
        &app.source_dir(video_id),
        "audio",
        AUDIO_FORMAT,
        log,
    )
    .await
}

pub async fn download_video(app: &App, video_id: &str, log: &JobLog) -> Result<PathBuf> {
    download(
        &watch_url(video_id),
        &app.source_dir(video_id),
        "video",
        VIDEO_FORMAT,
        log,
    )
    .await
}

/// Source ids to skip: saved clip selections, the queue, the channel's own
/// uploads, and the video links in each upload's description.
pub async fn processed_sources(app: &App, channel: &str) -> Result<BTreeSet<String>> {
    let name = channel.to_owned();
    let mut seen: BTreeSet<String> = app
        .db
        .call(move |c| {
            let mut statement = c.prepare(
                "SELECT source_id FROM selections WHERE channel = ?1
                 UNION SELECT source_id FROM clips WHERE channel = ?1",
            )?;
            let ids = statement
                .query_map([&name], |row| row.get(0))?
                .collect::<rusqlite::Result<_>>()?;
            Ok(ids)
        })
        .await?;
    static LINK: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})").unwrap()
    });
    match crate::youtube::channel_videos(app, channel, false).await {
        Ok(videos) => {
            for video in videos.iter() {
                // Our own uploads show up in searches too; never re-clip a clip.
                seen.insert(video.id.clone());
                seen.extend(
                    LINK.captures_iter(&video.description)
                        .map(|c| c[1].to_owned()),
                );
            }
        }
        Err(error) => tracing::warn!("Could not read channel descriptions: {error:#}"),
    }
    Ok(seen)
}

pub async fn find(
    app: &App,
    channel: &str,
    query: Option<&str>,
    channel_url: Option<&str>,
    limit: u32,
    newest: bool,
) -> Result<Vec<Value>> {
    let target = match (channel_url, query) {
        (Some(url), _) => url.to_owned(),
        (None, Some(query)) => {
            let mut url = url::Url::parse("https://www.youtube.com/results")?;
            url.query_pairs_mut().append_pair("search_query", query);
            if newest {
                url.query_pairs_mut().append_pair("sp", "CAI=");
            }
            url.to_string()
        }
        (None, None) => bail!("Pass a search query or a channel/playlist URL."),
    };
    let limit = limit.clamp(1, 200);
    let listing: Value = serde_json::from_slice(
        &yt_dlp(&[
            "-J",
            "--flat-playlist",
            "--playlist-end",
            &limit.to_string(),
            &target,
        ])
        .await?,
    )?;
    let done = processed_sources(app, channel).await?;
    Ok(listing["entries"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|e| e["ie_key"] == "Youtube")
        .take(limit as usize)
        .map(|e| {
            let id = e["id"].as_str().unwrap_or_default();
            json!({
                "url": watch_url(id),
                "title": e["title"],
                "channel": first(&[&e["channel"], &e["uploader"], &listing["channel"]]),
                "duration_minutes": (e["duration"].as_f64().unwrap_or(0.0) / 60.0).round(),
                "views": e["view_count"],
                "uploaded": first(&[&e["upload_date"], &e["timestamp"]]),
                "already_clipped": done.contains(id),
            })
        })
        .collect())
}

fn first(values: &[&Value]) -> Value {
    values
        .iter()
        .find(|v| !v.is_null())
        .map_or(Value::Null, |v| (*v).clone())
}

/// Best-effort caption preview. Long streams often have no captions, and
/// YouTube rate-limits caption downloads per IP.
async fn caption_text(app: &App, info: &Value, limit: usize) -> String {
    if limit == 0 {
        return String::new();
    }
    let track = ["subtitles", "automatic_captions"]
        .iter()
        .flat_map(|group| ["en", "en-orig", "en-US", "en-GB"].map(|lang| &info[group][lang]))
        .find_map(|tracks| tracks.as_array().filter(|t| !t.is_empty()))
        .and_then(|tracks| tracks.iter().find(|t| t["ext"] == "json3"));
    let Some(url) = track.and_then(|t| t["url"].as_str()) else {
        return "(no English captions; use sample_source to hear who is talking)".into();
    };
    let response = app
        .http
        .get(url)
        .header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36")
        .send()
        .await
        .and_then(reqwest::Response::error_for_status);
    let events: Value = match response {
        Ok(response) => match response.json().await {
            Ok(value) => value,
            Err(error) => {
                return format!("(captions unavailable: {error}; use sample_source instead)");
            }
        },
        Err(error) => return format!("(captions unavailable: {error}; use sample_source instead)"),
    };
    let text = events["events"]
        .as_array()
        .into_iter()
        .flatten()
        .flat_map(|e| e["segs"].as_array().into_iter().flatten())
        .filter_map(|s| s["utf8"].as_str())
        .map(str::trim)
        .collect::<Vec<_>>()
        .join(" ");
    text.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(limit)
        .collect()
}

pub async fn source_info(
    app: &App,
    channel: &str,
    url: &str,
    transcript_chars: usize,
) -> Result<Value> {
    let info = info(url).await?;
    let captions = caption_text(app, &info, transcript_chars).await;
    let was_live = info["was_live"].as_bool().unwrap_or(false);
    let id = info["id"].as_str().unwrap_or_default().to_owned();
    let recording = if was_live {
        info["release_date"].clone()
    } else {
        Value::Null
    };
    Ok(json!({
        "url": url,
        "title": info["title"],
        "channel": info["channel"],
        "uploaded": info["upload_date"],
        "recording_date": recording,
        "recording_date_basis": if recording.is_null() { Value::Null } else { "YouTube live release_date".into() },
        "was_live": info["was_live"],
        "duration_minutes": (info["duration"].as_f64().unwrap_or(0.0) / 60.0).round(),
        "views": info["view_count"],
        "description": info["description"].as_str().unwrap_or_default().chars().take(3000).collect::<String>(),
        "chapters": info["chapters"].as_array().into_iter().flatten().map(|c| json!({
            "start_minute": (c["start_time"].as_f64().unwrap_or(0.0) / 60.0).round(),
            "title": c["title"],
        })).collect::<Vec<_>>(),
        "already_clipped": processed_sources(app, channel).await?.contains(&id),
        "transcript_preview": captions,
    }))
}

/// Transcribe an interval the agent chose, to check who speaks.
pub async fn sample(app: &App, url: &str, start_seconds: f64, end_seconds: f64) -> Result<Value> {
    if !(0.0 <= start_seconds && start_seconds < end_seconds) || end_seconds - start_seconds > 900.0
    {
        bail!("Choose an interval of up to 15 minutes with 0 <= start < end.");
    }
    let info = info(url).await?;
    let duration = info["duration"].as_f64().unwrap_or(0.0);
    if duration > 0.0 && end_seconds > duration {
        bail!("The sample extends beyond the source video.");
    }
    let config = app.config()?;
    let work = std::env::temp_dir().join(format!("clipy-sample-{}", uuid::Uuid::new_v4().simple()));
    tokio::fs::create_dir_all(&work).await?;
    let result = async {
        let template = work.join("part.%(ext)s");
        let stdout = yt_dlp(&[
            "-f",
            "bestaudio/best",
            "--download-sections",
            &format!("*{start_seconds}-{end_seconds}"),
            "-o",
            &template.to_string_lossy(),
            "--print",
            "after_move:filepath",
            url,
        ])
        .await?;
        let part = PathBuf::from(
            String::from_utf8_lossy(&stdout)
                .trim()
                .lines()
                .last()
                .unwrap_or(""),
        );
        if !part.is_file() {
            bail!("yt-dlp downloaded no audio sample.");
        }
        crate::transcribe::transcribe(&app.http, &part, &config.speech_model).await
    }
    .await;
    let _ = tokio::fs::remove_dir_all(&work).await;
    let transcript = result?;
    let mut words: Vec<(String, usize)> = Vec::new();
    let mut lines = Vec::new();
    for u in transcript["utterances"].as_array().into_iter().flatten() {
        let speaker = u["speaker"].as_str().unwrap_or("?").to_owned();
        let text = u["text"].as_str().unwrap_or_default();
        match words.iter_mut().find(|(s, _)| *s == speaker) {
            Some((_, n)) => *n += text.split_whitespace().count(),
            None => words.push((speaker.clone(), text.split_whitespace().count())),
        }
        lines.push(format!("{speaker}: {text}"));
    }
    let total = words.iter().map(|(_, n)| n).sum::<usize>().max(1);
    words.sort_by_key(|(_, n)| std::cmp::Reverse(*n));
    Ok(json!({
        "title": info["title"],
        "channel": info["channel"],
        "sampled_at_minutes": [(start_seconds / 60.0).round()],
        "sampled_minutes": ((end_seconds - start_seconds) / 60.0).round(),
        "speakers": words.iter().map(|(speaker, n)| json!({
            "speaker": speaker,
            "share_of_words_percent": (100.0 * *n as f64 / total as f64).round(),
        })).collect::<Vec<_>>(),
        "transcript": lines.join("\n"),
    }))
}
