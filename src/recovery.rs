//! Repair only Clipy-owned, unwatched private uploads whose processing failed
//! or stalled; persist each transition so a retry resumes the same repair.

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use reqwest::Method;
use serde_json::{Value, json};

use crate::app::{App, now, parse_time, sydney};
use crate::jobs::JobLog;
use crate::queue::{self, Clip};
use crate::youtube::{self, API, iso_duration_seconds};

const PARTS: &str = "snippet,status,statistics,contentDetails,processingDetails";

pub async fn inspect(app: &App, channel: &str, video_id: &str) -> Result<Value> {
    youtube::videos(app, channel, &[video_id.to_owned()], PARTS)
        .await?
        .into_iter()
        .next()
        .with_context(|| format!("Video {video_id} is missing; do not guess its state."))
}

fn processed(video: &Value) -> bool {
    (video["processingDetails"]["processingStatus"] == "succeeded"
        || video["status"]["uploadStatus"] == "processed")
        && iso_duration_seconds(video["contentDetails"]["duration"].as_str().unwrap_or("")) > 0
}

fn views(video: &Value) -> i64 {
    video["statistics"]["viewCount"]
        .as_str()
        .and_then(|v| v.parse().ok())
        .unwrap_or(0)
}

/// Only a private, unwatched, unrejected upload that failed or has been
/// processing for at least a day may be replaced.
fn check_safe(video: &Value) -> Result<()> {
    let status = &video["status"];
    let processing = video["processingDetails"]["processingStatus"]
        .as_str()
        .unwrap_or("");
    if status["privacyStatus"] != "private" || views(video) > 0 {
        bail!("Only private uploads with zero views can be replaced.");
    }
    if status["uploadStatus"] == "rejected" || !status["rejectionReason"].is_null() {
        bail!("Rejected uploads need review; never re-upload to bypass a rejection.");
    }
    if processed(video) {
        bail!("Video has processed successfully; it does not need replacement.");
    }
    let failed =
        status["uploadStatus"] == "failed" || ["failed", "terminated"].contains(&processing);
    let uploaded = parse_time(video["snippet"]["publishedAt"].as_str().unwrap_or(""))?;
    let stale = processing == "processing"
        && now().duration_since(uploaded) >= jiff::SignedDuration::from_hours(24);
    if !(failed || stale) {
        bail!("Processing is not failed or stalled for at least 24 hours; wait.");
    }
    Ok(())
}

/// Refuse a slot another upload already occupies on the same Sydney day.
async fn check_slot(
    app: &App,
    channel: &str,
    target: jiff::Timestamp,
    excluded: &BTreeSet<String>,
) -> Result<()> {
    let day = target.to_zoned(sydney()).date();
    for video in youtube::channel_videos(app, channel, true).await?.iter() {
        if !excluded.contains(&video.id) && youtube::sydney_day(&video.publish_time)? == day {
            bail!("Another upload already occupies Sydney day {day}; choose an empty slot.");
        }
    }
    Ok(())
}

fn private_status(original: &Value, publish_at: Option<&str>) -> Value {
    let mut status = json!({});
    for key in [
        "license",
        "embeddable",
        "publicStatsViewable",
        "selfDeclaredMadeForKids",
    ] {
        if let Some(value) = original.get(key) {
            status[key] = value.clone();
        }
    }
    status["privacyStatus"] = "private".into();
    if let Some(when) = publish_at {
        status["publishAt"] = when.into();
    }
    status
}

/// Rebuild the clip from a fresh download with a full re-encode, which drops
/// the original MP4's stream and timestamp faults.
async fn rebuild(app: &App, clip: &mut Clip, log: &JobLog) -> Result<()> {
    let (_, plans) = crate::pipeline::saved_selections(app, &clip.channel, &clip.source_id)
        .await?
        .context("The clip's saved selections are missing; it cannot be rebuilt.")?;
    let plan = plans
        .get((clip.number - 1) as usize)
        .context("The clip's saved selection is missing.")?;
    let video = crate::sources::download_video(app, &clip.source_id, log).await?;
    let audio = crate::sources::download_audio(app, &clip.source_id, log).await?;
    let report = crate::render::render(
        &video,
        &audio,
        plan,
        &clip.video_path(app),
        &clip.dir(app).join("assets"),
        &app.http,
    )
    .await
    .context("Could not rebuild the clip; the original upload is unchanged.")?;
    clip.start_ms = plan.start_ms;
    clip.end_ms = plan.end_ms;
    clip.source = plan.source.clone();
    clip.edit_report = Some(report);
    Ok(())
}

pub async fn repair(
    app: &App,
    channel: &str,
    video_id: &str,
    publish_at: Option<String>,
    log: &JobLog,
) -> Result<Value> {
    let mut clip = {
        let (channel, video) = (channel.to_owned(), video_id.to_owned());
        app.db
            .call(move |c| queue::by_video(c, &channel, &video))
            .await?
            .context("Only uploads linked to this channel's Clipy queue can be repaired.")?
    };
    if clip
        .repair
        .as_ref()
        .is_some_and(|r| !r["completed_at"].is_null())
    {
        return Ok(clip.youtube_url().into());
    }
    if clip.repair.is_none() {
        let original = inspect(app, channel, video_id).await?;
        check_safe(&original)?;
        let when = publish_at
            .or_else(|| original["status"]["publishAt"].as_str().map(str::to_owned))
            .context("Supply a future publish_at for this unscheduled upload.")?;
        let target: jiff::Timestamp = when.parse().context(
            "Replacement publish_at needs a timezone and must be 12 hours to 8 days ahead.",
        )?;
        let ahead = target.duration_since(now());
        if ahead < jiff::SignedDuration::from_hours(12)
            || ahead > jiff::SignedDuration::from_hours(24 * 8)
        {
            bail!("Replacement publish_at needs a timezone and must be 12 hours to 8 days ahead.");
        }
        check_slot(app, channel, target, &BTreeSet::from([video_id.to_owned()])).await?;
        clip.repair = Some(json!({
            "original_id": video_id,
            "publish_at": target.to_string(),
            "started_at": now().to_string(),
            "snippet": original["snippet"],
            "original_status": original["status"],
        }));
        queue::store(app, &clip).await?;
    }
    let mut state = clip.repair.clone().unwrap_or_default();
    let original_id = state["original_id"].as_str().unwrap_or_default().to_owned();
    if state["replacement_id"].is_null() {
        check_safe(&inspect(app, channel, &original_id).await?)?;
        if state["upload_started"] == true {
            // Upload completion can be lost even after every session retry.
            // Find the replacement by its persisted unique tag before inserting.
            let marker = state["marker"].as_str().unwrap_or_default().to_owned();
            let videos = youtube::channel_videos(app, channel, true).await?;
            let matches: Vec<_> = videos.iter().filter(|v| v.tags.contains(&marker)).collect();
            if matches.len() != 1 {
                bail!("Replacement upload outcome is uncertain; inspect YouTube before retrying.");
            }
            state["replacement_id"] = matches[0].id.clone().into();
            clip.repair = Some(state.clone());
            queue::store(app, &clip).await?;
        } else {
            rebuild(app, &mut clip, log).await?;
            let marker = format!("clipy-repair-{original_id}");
            state["marker"] = marker.clone().into();
            state["upload_started"] = true.into();
            clip.repair = Some(state.clone());
            queue::store(app, &clip).await?;
            let snippet = &state["snippet"];
            let mut tags: Vec<String> =
                serde_json::from_value(snippet["tags"].clone()).unwrap_or_default();
            tags.push(marker);
            // Preserve the live description, including changes made since upload.
            let description =
                crate::publish::full_description(&clip, None, snippet["description"].as_str());
            let mut replacement = clip.clone();
            replacement.publish_at = None;
            log.info("Uploading a private replacement");
            let id = crate::publish::upload(
                app,
                &mut replacement,
                snippet["title"].as_str().unwrap_or(&clip.title),
                description,
                snippet["categoryId"].as_str().unwrap_or("22"),
                &tags,
                true,
            )
            .await?;
            state["replacement_id"] = id.clone().into();
            clip.repair = Some(state.clone());
            clip.thumbnail_pending = true;
            queue::store(app, &clip).await?;
            let mut uploaded = clip.clone();
            uploaded.youtube_id = Some(id);
            crate::publish::set_thumbnail(app, &mut uploaded).await;
            clip.thumbnail_pending = uploaded.thumbnail_pending;
            queue::store(app, &clip).await?;
        }
    }
    let replacement_id = state["replacement_id"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    let deadline = Instant::now() + Duration::from_secs(600);
    let replacement = loop {
        let replacement = inspect(app, channel, &replacement_id).await?;
        if processed(&replacement) {
            break replacement;
        }
        let processing = replacement["processingDetails"]["processingStatus"]
            .as_str()
            .unwrap_or("");
        if ["failed", "rejected"]
            .contains(&replacement["status"]["uploadStatus"].as_str().unwrap_or(""))
            || ["failed", "terminated"].contains(&processing)
        {
            bail!("Replacement processing failed; originals and local media are retained.");
        }
        if Instant::now() >= deadline {
            bail!(
                "Replacement is still processing; call repair_upload again to resume this same replacement."
            );
        }
        tokio::time::sleep(Duration::from_secs(15)).await;
    };
    // Finish only while both copies are private and unwatched. Persist the new
    // identity before deletion so a lost delete never triggers a re-upload.
    if state["activated"] != true {
        let original = inspect(app, channel, &original_id).await?;
        check_safe(&original)?;
        if replacement["status"]["privacyStatus"] != "private" || views(&replacement) > 0 {
            bail!("Replacement is no longer private and unwatched; inspect before proceeding.");
        }
        let slot_text = state["publish_at"].as_str().unwrap_or_default().to_owned();
        let slot = parse_time(&slot_text)?;
        if slot <= now() {
            bail!("The release slot has passed; inspect and choose a new slot before activating.");
        }
        check_slot(
            app,
            channel,
            slot,
            &BTreeSet::from([original_id.clone(), replacement_id.clone()]),
        )
        .await?;
        let update = |body: Value, part: &'static str| async move {
            youtube::request(
                app,
                channel,
                Method::PUT,
                &format!("{API}/videos"),
                &[("part", part.into())],
                Some(&body),
            )
            .await
        };
        update(
            json!({"id": original_id, "status": private_status(&original["status"], None)}),
            "status",
        )
        .await?;
        let saved_snippet = &state["snippet"];
        let mut snippet = json!({});
        for key in ["title", "description", "categoryId", "tags"] {
            if let Some(value) = saved_snippet.get(key) {
                snippet[key] = value.clone();
            }
        }
        let original_status = if state["original_status"].is_null() {
            &original["status"]
        } else {
            &state["original_status"]
        };
        update(
            json!({"id": replacement_id, "snippet": snippet, "status": private_status(original_status, Some(&slot_text))}),
            "snippet,status",
        )
        .await?;
        clip.youtube_id = Some(replacement_id.clone());
        clip.publish_at = Some(slot_text);
        clip.status = "uploaded".into();
        clip.processing_status = Some("succeeded".into());
        state["activated"] = true.into();
        clip.repair = Some(state.clone());
        queue::store(app, &clip).await?;
    }
    let old = youtube::videos(app, channel, std::slice::from_ref(&original_id), PARTS).await?;
    if let Some(old) = old.first() {
        check_safe(old)?;
        if !processed(&inspect(app, channel, &replacement_id).await?) {
            bail!("Replacement processing is no longer successful; retain original.");
        }
        youtube::request(
            app,
            channel,
            Method::DELETE,
            &format!("{API}/videos"),
            &[("id", original_id.clone())],
            None,
        )
        .await?;
    }
    state["completed_at"] = now().to_string().into();
    clip.repair = Some(state);
    queue::store(app, &clip).await?;
    let _ = tokio::fs::remove_file(clip.video_path(app)).await;
    log.info(format!("Replaced {original_id} with {replacement_id}"));
    Ok(clip.youtube_url().into())
}
