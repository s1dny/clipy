//! Uploading queued clips. Upload success is distinct from processing
//! success: the MP4 is kept until YouTube confirms the video processed.

use std::path::Path;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};

use crate::app::{App, now};
use crate::clips::{MIN_VIDEO_MS, chapters_text};
use crate::jobs::JobLog;
use crate::queue::{self, Clip};
use crate::youtube;

const CHUNK: usize = 10 << 20;

/// Fill `{clip_description}` and `{original_url}`; `{{` and `}}` are braces.
pub fn fill(template: &str, values: &[(&str, &str)]) -> String {
    let mut out = String::new();
    let mut rest = template;
    while let Some(i) = rest.find(['{', '}']) {
        out.push_str(&rest[..i]);
        rest = &rest[i..];
        if rest.starts_with("{{") || rest.starts_with("}}") {
            out.push_str(&rest[..1]);
            rest = &rest[2..];
            continue;
        }
        if let Some(end) = rest.find('}').filter(|_| rest.starts_with('{')) {
            let name = &rest[1..end];
            if let Some((_, value)) = values.iter().find(|(key, _)| *key == name) {
                out.push_str(value);
                rest = &rest[end + 1..];
                continue;
            }
        }
        out.push_str(&rest[..1]);
        rest = &rest[1..];
    }
    out.push_str(rest);
    out
}

/// The clip's description with its chapters and source, wrapped in the
/// channel's template. `template` None keeps a live description verbatim.
pub fn full_description(clip: &Clip, template: Option<&str>, live: Option<&str>) -> String {
    let mut body = clip.description.clone();
    if !clip.chapters.is_empty() {
        body += &format!("\n\nChapters\n{}", chapters_text(&clip.chapters));
    }
    let original_url = format!(
        "https://youtube.com/watch?v={}&t={}s",
        clip.source_id,
        clip.start_ms / 1000
    );
    let mut description = match (template, live) {
        (_, Some(live)) => live.to_owned(),
        (Some(template), None) => fill(
            template,
            &[("clip_description", &body), ("original_url", &original_url)],
        ),
        (None, None) => body,
    };
    // Templates cannot omit source attribution.
    let name = clip.source.as_ref().map(|s| s.name.as_str()).unwrap_or("");
    let attribution = format!("Source: {name} {original_url}").replace("  ", " ");
    if !description.contains(&attribution) {
        description += &format!("\n\n{attribution}");
    }
    if let Some(source) = &clip.source {
        let streamed = format!("Streamed: {}", source.streamed_on);
        if !description.contains(&streamed) {
            description += &format!("\n{streamed}");
        }
    }
    let mut credits: Vec<String> = Vec::new();
    for card in clip
        .edit_report
        .as_ref()
        .and_then(|r| r["annotations"].as_array())
        .into_iter()
        .flatten()
    {
        if let (Some(url), Some(credit)) =
            (card["image_url"].as_str(), card["image_credit"].as_str())
        {
            let line = format!("{credit} {url}");
            if !credits.contains(&line) {
                credits.push(line);
            }
        }
    }
    if !credits.is_empty() && !description.contains("\n\nImages:\n") {
        description += &format!("\n\nImages:\n{}", credits.join("\n"));
    }
    description.replace('\u{2014}', ", ")
}

/// Check a clip may be published before any request reaches YouTube.
async fn check_release(app: &App, clip: &Clip, video: &Path) -> Result<()> {
    if clip.title.contains('\u{2014}') {
        bail!("Em dashes are not allowed in video titles.");
    }
    let source = clip.source.as_ref().context(
        "Every video needs its verified stream date and source. Re-render this pending clip with edit_video first.",
    )?;
    let rendered = clip
        .edit_report
        .as_ref()
        .map(|r| r["source"].clone())
        .unwrap_or_default();
    if rendered != serde_json::to_value(source)? {
        bail!(
            "Every video must be rendered with its verified stream date and source visible. Re-render this pending clip first."
        );
    }
    let ffmpeg = app.config()?.ffmpeg;
    let media = crate::render::media_info(video, &ffmpeg).await?;
    let duration = |kind: &str| -> f64 {
        media["streams"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|s| s["codec_type"] == kind)
            .and_then(|s| s["duration"].as_str()?.parse().ok())
            .unwrap_or(0.0)
    };
    let video_seconds = duration("video");
    if video_seconds * 1000.0 < MIN_VIDEO_MS as f64 || duration("audio") < video_seconds - 0.25 {
        bail!(
            "Every video must be at least 8 minutes after editing; the actual rendered audio/video is too short."
        );
    }
    Ok(())
}

/// Upload a file through a resumable session. A dropped connection resumes
/// the same session after asking how much arrived, so it can never insert a
/// second video.
pub async fn resumable_upload(
    app: &App,
    channel: &str,
    file: &Path,
    metadata: &Value,
) -> Result<Value> {
    let bytes = tokio::fs::read(file).await?;
    let total = bytes.len();
    let token = youtube::access_token(app, channel).await?;
    let response = app
        .http
        .post("https://youtube.googleapis.com/upload/youtube/v3/videos")
        .query(&[("uploadType", "resumable"), ("part", "snippet,status")])
        .bearer_auth(&token)
        .header("X-Upload-Content-Type", "video/mp4")
        .header("X-Upload-Content-Length", total.to_string())
        .json(metadata)
        .send()
        .await?;
    if !response.status().is_success() {
        youtube::read_json(response).await?;
        unreachable!();
    }
    let session = response
        .headers()
        .get("location")
        .and_then(|v| v.to_str().ok())
        .context("YouTube returned no upload session")?
        .to_owned();
    let mut offset = 0;
    let mut failures = 0;
    loop {
        let end = (offset + CHUNK).min(total);
        let attempt = app
            .http
            .put(&session)
            .bearer_auth(&token)
            .header(
                "Content-Range",
                format!("bytes {offset}-{}/{total}", end.max(1) - 1),
            )
            .timeout(Duration::from_secs(600))
            .body(bytes[offset..end].to_vec())
            .send()
            .await;
        let response = match attempt {
            Ok(response) => response,
            Err(error) => {
                if failures >= 5 {
                    return Err(error.into());
                }
                failures += 1;
                tracing::warn!(
                    "Upload connection interrupted; resuming the same session (attempt {failures}/5)"
                );
                tokio::time::sleep(Duration::from_secs(1 << (failures - 1))).await;
                offset = received(app, &session, &token, total).await?;
                continue;
            }
        };
        failures = 0;
        match response.status().as_u16() {
            200 | 201 => return youtube::read_json(response).await,
            308 => offset = next_offset(&response),
            _ => {
                youtube::read_json(response).await?;
                unreachable!();
            }
        }
    }
}

/// Ask the session how many bytes it has.
async fn received(app: &App, session: &str, token: &str, total: usize) -> Result<usize> {
    let response = app
        .http
        .put(session)
        .bearer_auth(token)
        .header("Content-Range", format!("bytes */{total}"))
        .body(Vec::new())
        .send()
        .await?;
    match response.status().as_u16() {
        308 => Ok(next_offset(&response)),
        status => bail!(
            "Upload session status check returned HTTP {status}; inspect YouTube before retrying."
        ),
    }
}

fn next_offset(response: &reqwest::Response) -> usize {
    response
        .headers()
        .get("range")
        .and_then(|v| v.to_str().ok())
        .and_then(|range| range.rsplit('-').next()?.parse::<usize>().ok())
        .map_or(0, |last| last + 1)
}

/// Upload a clip with its thumbnail: private and scheduled when it has
/// `publish_at`, otherwise public straight away. `on_uploaded` runs once the
/// video id is known, before the thumbnail.
pub async fn upload(
    app: &App,
    clip: &mut Clip,
    title: &str,
    description: String,
    category_id: &str,
    tags: &[String],
    private: bool,
) -> Result<String> {
    let video = clip.video_path(app);
    check_release(app, clip, &video).await?;
    let status = match &clip.publish_at {
        Some(when) => json!({"privacyStatus": "private", "publishAt": when}),
        None => json!({"privacyStatus": if private { "private" } else { "public" }}),
    };
    let mut snippet =
        json!({"title": title, "description": description, "categoryId": category_id});
    if !tags.is_empty() {
        snippet["tags"] = json!(tags);
    }
    tracing::info!(
        "Uploading {} ({})",
        title,
        clip.publish_at
            .as_deref()
            .unwrap_or(if private { "private" } else { "public" })
    );
    let response = resumable_upload(
        app,
        &clip.channel,
        &video,
        &json!({"snippet": snippet, "status": status}),
    )
    .await?;
    let id = response["id"]
        .as_str()
        .context("YouTube returned no video id")?
        .to_owned();
    tracing::info!("Uploaded https://youtube.com/watch?v={id}");
    Ok(id)
}

/// Set an uploaded clip's thumbnail. On failure (YouTube rate-limits
/// thumbnails per account) the clip is marked thumbnail_pending.
pub async fn set_thumbnail(app: &App, clip: &mut Clip) -> bool {
    let Some(video) = clip.youtube_id.clone() else {
        return false;
    };
    match youtube::set_thumbnail(app, &clip.channel, &video, &clip.thumbnail_path(app)).await {
        Ok(()) => {
            clip.thumbnail_pending = false;
            true
        }
        Err(error) => {
            tracing::warn!("Could not set thumbnail (will retry): {error:#}");
            clip.thumbnail_pending = true;
            false
        }
    }
}

async fn save(app: &App, clip: &Clip) -> Result<()> {
    let clip = clip.clone();
    app.db.call(move |c| clip.save(c)).await
}

/// The publish job: upload a pending clip, or reconcile a failed one with
/// YouTube before ever starting another upload session.
pub async fn publish(
    app: &App,
    channel: &str,
    clip_id: &str,
    publish_at: Option<String>,
    log: &JobLog,
) -> Result<Value> {
    let mut clip = {
        let (channel, id) = (channel.to_owned(), clip_id.to_owned());
        app.db.call(move |c| queue::get(c, &channel, &id)).await?
    };
    if clip.status == "failed" || clip.youtube_id.is_some() {
        // A lost response can follow a successful upload.
        let videos = youtube::channel_videos(app, channel, true).await?;
        let matches: Vec<_> = videos
            .iter()
            .filter(|v| {
                Some(&v.id) == clip.youtube_id.as_ref()
                    || (v.title == clip.title && v.description.contains(&clip.source_id))
            })
            .collect();
        if matches.len() > 1 {
            bail!("Multiple YouTube uploads match this failed clip; inspect them before retrying.");
        }
        if let Some(video) = matches.first() {
            let upload_status = video.upload_status.as_deref().unwrap_or("");
            if !["uploaded", "processed"].contains(&upload_status) {
                bail!(
                    "A matching YouTube upload is not complete; check its status before retrying."
                );
            }
            clip.youtube_id = Some(video.id.clone());
            set_thumbnail(app, &mut clip).await;
            clip.status = "uploaded".into();
            clip.uploaded_at = Some(now().to_string());
            save(app, &clip).await?;
            if upload_status == "processed" {
                let _ = tokio::fs::remove_file(clip.video_path(app)).await;
            }
            log.info("Reconciled the clip with its existing upload");
            return Ok(clip.youtube_url().into());
        }
    } else if clip.status != "pending" {
        bail!("Clip is {}, not pending or failed.", clip.status);
    }
    clip.publish_at = publish_at;
    let config = app.channel_config(channel)?;
    let description = full_description(&clip, Some(&config.description_template), None);
    let (title, tags) = (clip.title.clone(), clip.tags.clone());
    match upload(
        app,
        &mut clip,
        &title,
        description,
        &config.category_id,
        &tags,
        false,
    )
    .await
    {
        Ok(id) => {
            clip.youtube_id = Some(id);
            clip.status = "uploaded".into();
            clip.uploaded_at = Some(now().to_string());
            save(app, &clip).await?;
            set_thumbnail(app, &mut clip).await;
            save(app, &clip).await?;
            log.info(format!(
                "Uploaded {}",
                clip.youtube_url().unwrap_or_default()
            ));
            // Keep the MP4 until a later processing check confirms it plays.
            Ok(clip.youtube_url().into())
        }
        Err(error) => {
            clip.status = "failed".into();
            save(app, &clip).await?;
            Err(error)
        }
    }
}

/// Retry thumbnails YouTube refused at upload, stopping at the first refusal.
pub async fn retry_thumbnails(app: &App, channel: &str) -> Result<Value> {
    let name = channel.to_owned();
    let pending: Vec<Clip> = app
        .db
        .call(move |c| {
            Ok(queue::list(c, &name, "all")?
                .into_iter()
                .filter(|clip| clip.thumbnail_pending && clip.youtube_id.is_some())
                .collect())
        })
        .await?;
    let mut done = 0;
    for mut clip in pending.clone() {
        let ok = set_thumbnail(app, &mut clip).await;
        save(app, &clip).await?;
        if !ok {
            break;
        }
        done += 1;
    }
    Ok(format!("Set {done} of {} pending thumbnails.", pending.len()).into())
}

/// Refresh local upload state from YouTube, and reclaim each MP4 once its
/// video has processed.
pub async fn reconcile(app: &App, channel: &str, videos: &[youtube::Video]) -> Result<()> {
    let name = channel.to_owned();
    let clips: Vec<Clip> = app.db.call(move |c| queue::list(c, &name, "all")).await?;
    for mut clip in clips {
        let Some(video) = videos
            .iter()
            .find(|v| Some(&v.id) == clip.youtube_id.as_ref())
        else {
            continue;
        };
        let (publish, processing) = (
            Some(video.publish_time.clone()),
            video.processing_status.clone(),
        );
        if clip.publish_at != publish || clip.processing_status != processing {
            clip.publish_at = publish;
            clip.processing_status = processing;
            save(app, &clip).await?;
        }
        if video.upload_status.as_deref() == Some("processed") && video.duration_seconds > 0 {
            let _ = tokio::fs::remove_file(clip.video_path(app)).await;
        }
    }
    Ok(())
}
