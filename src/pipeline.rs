//! Prepare source transcripts, then render and queue the agent's editorial
//! choices. The server makes no editorial model calls.

use anyhow::{Context, Result, bail};
use futures::StreamExt;
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

use crate::app::{App, now, video_id};
use crate::clips::ClipSelection;
use crate::jobs::JobLog;
use crate::queue::{self, Clip};
use crate::render;
use crate::sources::{download_audio, download_video, downloaded};
use crate::transcribe::{self, Sentence};

async fn sentences(app: &App, source: &str) -> Result<Option<Vec<Sentence>>> {
    let source = source.to_owned();
    app.db
        .call(move |c| {
            let text: Option<String> = c
                .query_row(
                    "SELECT sentences FROM transcripts WHERE video_id = ?1",
                    [&source],
                    |r| r.get(0),
                )
                .optional()?;
            Ok(text.map(|t| serde_json::from_str(&t)).transpose()?)
        })
        .await
}

/// Prepare the complete timestamped transcript without choosing any clips.
pub async fn prepare(app: &App, url: &str, include_video: bool, log: &JobLog) -> Result<Value> {
    let source = video_id(url)?;
    let sentences = match sentences(app, &source).await? {
        Some(sentences) => sentences,
        None => {
            let audio = download_audio(app, &source, log).await?;
            log.info("Transcribing audio with AssemblyAI");
            let transcript =
                transcribe::transcribe(&app.http, &audio, &app.config()?.speech_model).await?;
            let sentences = transcribe::sentences(&transcript);
            let (id, text) = (source.clone(), serde_json::to_string(&sentences)?);
            app.db
                .call(move |c| {
                    c.execute(
                        "INSERT OR REPLACE INTO transcripts (video_id, sentences, created_at) VALUES (?1, ?2, ?3)",
                        params![id, text, now().to_string()],
                    )?;
                    Ok(())
                })
                .await?;
            log.info(format!("Transcribed {} sentences", sentences.len()));
            sentences
        }
    };
    if include_video {
        download_video(app, &source, log).await?;
    }
    Ok(json!({
        "url": url,
        "sentence_count": sentences.len(),
        "video_available": downloaded(&app.source_dir(&source), "video").is_some(),
    }))
}

/// Lossless pages; next_offset is null only when the end has been reached.
pub async fn transcript_page(app: &App, url: &str, offset: usize, limit: usize) -> Result<Value> {
    if !(1..=1000).contains(&limit) {
        bail!("offset must be nonnegative and limit between 1 and 1000.");
    }
    let sentences = sentences(app, &video_id(url)?)
        .await?
        .context("Call prepare_source and let its job finish first.")?;
    let stop = (offset + limit).min(sentences.len());
    let page = sentences.get(offset.min(stop)..stop).unwrap_or_default();
    Ok(json!({
        "url": url,
        "sentences": page,
        "sentence_count": sentences.len(),
        "next_offset": if stop < sentences.len() { Some(stop) } else { None },
    }))
}

/// The saved selections for a source, and their speaker.
pub async fn saved_selections(
    app: &App,
    channel: &str,
    source: &str,
) -> Result<Option<(String, Vec<ClipSelection>)>> {
    let (channel, source) = (channel.to_owned(), source.to_owned());
    app.db
        .call(move |c| {
            let row: Option<(String, String)> = c
                .query_row(
                    "SELECT speaker, clips FROM selections WHERE channel = ?1 AND source_id = ?2",
                    [&channel, &source],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()?;
            row.map(|(speaker, clips)| {
                Ok::<_, anyhow::Error>((speaker, serde_json::from_str(&clips)?))
            })
            .transpose()
        })
        .await
}

async fn save_selections(
    app: &App,
    channel: &str,
    source: &str,
    speaker: &str,
    clips: &[ClipSelection],
) -> Result<()> {
    let row = (
        channel.to_owned(),
        source.to_owned(),
        speaker.to_owned(),
        serde_json::to_string(clips)?,
    );
    app.db
        .call(move |c| {
            c.execute(
                "INSERT INTO selections (channel, source_id, speaker, clips, created_at) VALUES (?1, ?2, ?3, ?4, ?5)
                 ON CONFLICT (channel, source_id) DO UPDATE SET speaker = excluded.speaker, clips = excluded.clips",
                params![row.0, row.1, row.2, row.3, now().to_string()],
            )?;
            Ok(())
        })
        .await
}

/// Render submitted choices, or resume the saved choices.
pub async fn process(
    app: &App,
    channel: &str,
    url: &str,
    speaker: &str,
    submitted: Option<Vec<ClipSelection>>,
    log: &JobLog,
) -> Result<usize> {
    let source = video_id(url)?;
    let clips = match (saved_selections(app, channel, &source).await?, submitted) {
        (Some((_, saved)), submitted) => {
            // Media and queue identity are positional. An in-flight or resumed
            // job cannot silently replace the intervals under existing exports.
            if submitted.is_some_and(|submitted| submitted != saved) {
                bail!(
                    "Selections already saved; resume without clips. Use edit_clip for queued metadata."
                );
            }
            saved
        }
        (None, None) => bail!("No saved selections. Read the transcript and submit clips first."),
        (None, Some(submitted)) => {
            let sentences = sentences(app, &source)
                .await?
                .context("Prepare and read the source transcript before submitting clips.")?;
            let duration = sentences.iter().map(|s| s.end).max().unwrap_or(0);
            for clip in &submitted {
                clip.validate()?;
                if clip.end_ms > duration {
                    bail!("A clip extends beyond the timestamped transcript.");
                }
            }
            save_selections(app, channel, &source, speaker, &submitted).await?;
            submitted
        }
    };
    if clips.is_empty() {
        return Ok(0);
    }
    let config = app.config()?;
    let audio = download_audio(app, &source, log).await?;
    let mut failed = Vec::new();
    for attempt in 0..2 {
        failed.clear();
        let mut video = None;
        for (index, clip) in clips.iter().enumerate() {
            let number = index as i64 + 1;
            let output = app.clip_dir(channel, &source, number).join("video.mp4");
            if output.exists() {
                ensure_row(app, channel, &source, number, speaker, clip, &config.ffmpeg).await?;
                continue;
            }
            if video.is_none() {
                video = Some(download_video(app, &source, log).await?);
            }
            log.info(format!("Rendering clip {number}: {}", clip.title));
            let assets = app.clip_dir(channel, &source, number).join("assets");
            match render::render(
                video.as_ref().unwrap(),
                &audio,
                clip,
                &output,
                &assets,
                &app.http,
                &config.ffmpeg,
            )
            .await
            {
                Ok(report) => {
                    insert(app, channel, &source, number, speaker, clip, report).await?;
                }
                Err(error) => {
                    log.info(format!("Rendering clip {number} failed: {error:#}"));
                    failed.push(number);
                }
            }
        }
        if failed.is_empty() || attempt == 1 {
            break;
        }
        // A truncated or corrupt download fails renders; fetch it again once.
        log.info("Some clips failed to render; downloading the video again");
        if let Some(media) = downloaded(&app.source_dir(&source), "video") {
            tokio::fs::remove_file(media).await?;
        }
    }
    let missing_art = draw_thumbnails(app, channel, &source, clips.len(), log).await;
    if failed.is_empty() && missing_art == 0 {
        // Preserve source media on partial failure so resumption can finish it.
        for stem in ["audio", "video"] {
            if let Some(media) = downloaded(&app.source_dir(&source), stem) {
                tokio::fs::remove_file(media).await?;
            }
        }
    } else {
        bail!("Some exports or thumbnails failed; resume the saved selections.");
    }
    log.info(format!("Done: {} clips", clips.len()));
    Ok(clips.len())
}

/// A rendered export whose queue row was lost (a crash between the two)
/// gets its row back, with a freshly verified report.
async fn ensure_row(
    app: &App,
    channel: &str,
    source: &str,
    number: i64,
    speaker: &str,
    clip: &ClipSelection,
    ffmpeg: &str,
) -> Result<()> {
    let id = format!("{source}:{number:02}");
    let exists = {
        let (channel, id) = (channel.to_owned(), id.clone());
        app.db
            .call(move |c| Ok(queue::get(c, &channel, &id).is_ok()))
            .await?
    };
    if !exists {
        let mut report = render::edit_report(clip);
        let output = app.clip_dir(channel, source, number).join("video.mp4");
        report["verification"] = render::verify(&output, clip.duration_ms(), ffmpeg).await?;
        insert(app, channel, source, number, speaker, clip, report).await?;
    }
    Ok(())
}

async fn insert(
    app: &App,
    channel: &str,
    source: &str,
    number: i64,
    speaker: &str,
    clip: &ClipSelection,
    report: Value,
) -> Result<()> {
    let row = Clip {
        channel: channel.to_owned(),
        source_id: source.to_owned(),
        number,
        status: "pending".into(),
        title: clip.title.clone(),
        description: clip.description.clone(),
        speaker: Some(speaker.to_owned()),
        tags: Vec::new(),
        thumbnail_prompt: Some(clip.thumbnail_prompt.clone()),
        chapters: clip.chapters.clone(),
        score: clip.score,
        start_ms: clip.start_ms,
        end_ms: clip.end_ms,
        duration_ms: clip.duration_ms(),
        source: clip.source.clone(),
        edit_report: Some(report),
        created_at: now().to_string(),
        discard_reason: None,
        youtube_id: None,
        publish_at: None,
        uploaded_at: None,
        processing_status: None,
        thumbnail_pending: false,
        repair: None,
    };
    app.db
        .call(move |c| {
            if queue::get(c, &row.channel, &row.id()).is_err() {
                row.save(c)?;
            }
            Ok(())
        })
        .await
}

/// Draw every queued clip's missing thumbnail, ten at a time. Returns how
/// many are still missing.
async fn draw_thumbnails(
    app: &App,
    channel: &str,
    source: &str,
    count: usize,
    log: &JobLog,
) -> usize {
    let work = futures::stream::iter(1..=count as i64)
        .map(|number| async move {
            let id = format!("{source}:{number:02}");
            let clip = {
                let (channel, id) = (channel.to_owned(), id.clone());
                match app.db.call(move |c| queue::get(c, &channel, &id)).await {
                    Ok(clip) => clip,
                    Err(_) => return false,
                }
            };
            let path = clip.thumbnail_path(app);
            if path.exists() {
                return true;
            }
            let prompt = clip
                .thumbnail_prompt
                .clone()
                .unwrap_or_else(|| clip.title.clone());
            match crate::thumbnails::draw(
                app,
                channel,
                prompt.trim(),
                clip.speaker.as_deref(),
                &path,
            )
            .await
            {
                Ok(()) => true,
                Err(error) => {
                    log.info(format!("Thumbnail for {id} failed: {error:#}"));
                    false
                }
            }
        })
        .buffer_unordered(10)
        .collect::<Vec<bool>>()
        .await;
    work.iter().filter(|done| !**done).count()
}

/// Re-render one pending video with revised edits. The old export stays
/// until the new one is verified.
pub async fn edit_pending(
    app: &App,
    channel: &str,
    clip_id: &str,
    changes: &Value,
    log: &JobLog,
) -> Result<Value> {
    let clip = {
        let (channel, id) = (channel.to_owned(), clip_id.to_owned());
        app.db
            .call(move |c| queue::pending(c, &channel, &id))
            .await?
    };
    let (speaker, mut plans) = saved_selections(app, channel, &clip.source_id)
        .await?
        .context("The clip's saved selections are missing.")?;
    let index = (clip.number - 1) as usize;
    let mut plan = serde_json::to_value(
        plans
            .get(index)
            .context("The clip's saved selection is missing.")?,
    )?;
    for (key, value) in changes.as_object().into_iter().flatten() {
        plan[key] = value.clone();
    }
    plan["title"] = clip.title.clone().into();
    plan["description"] = clip.description.clone().into();
    if let Some(prompt) = &clip.thumbnail_prompt {
        plan["thumbnail_prompt"] = prompt.clone().into();
    }
    let selection: ClipSelection = serde_json::from_value(plan)?;
    selection.validate_release()?;
    let video = download_video(app, &clip.source_id, log).await?;
    let audio = download_audio(app, &clip.source_id, log).await?;
    let output = clip.video_path(app);
    let edited = output.with_file_name("edited.mp4");
    log.info(format!("Re-rendering {clip_id}"));
    let config = app.config()?;
    let report = render::render(
        &video,
        &audio,
        &selection,
        &edited,
        &clip.dir(app).join("assets"),
        &app.http,
        &config.ffmpeg,
    )
    .await?;
    tokio::fs::rename(&edited, &output).await?;
    plans[index] = selection.clone();
    save_selections(app, channel, &clip.source_id, &speaker, &plans).await?;
    let mut clip = clip;
    clip.source = selection.source.clone();
    clip.chapters = selection.chapters.clone();
    clip.duration_ms = selection.duration_ms();
    clip.edit_report = Some(report);
    app.db.call(move |c| clip.save(c)).await?;
    Ok(clip_id.into())
}
