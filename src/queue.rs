//! The upload queue: every rendered clip and where it is in its life.
//! A clip's id is "<source video id>:<two-digit number>".

use std::path::PathBuf;

use anyhow::{Context, Result, bail};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};

use crate::app::{App, watch_url};
use crate::clips::{Chapter, SourceAttribution};

#[derive(Debug, Clone)]
pub struct Clip {
    pub channel: String,
    pub source_id: String,
    pub number: i64,
    pub status: String,
    pub title: String,
    pub description: String,
    pub speaker: Option<String>,
    pub tags: Vec<String>,
    pub thumbnail_prompt: Option<String>,
    pub chapters: Vec<Chapter>,
    pub score: Option<f64>,
    pub start_ms: i64,
    pub end_ms: i64,
    pub duration_ms: i64,
    pub source: Option<SourceAttribution>,
    pub edit_report: Option<Value>,
    pub created_at: String,
    pub discard_reason: Option<String>,
    pub youtube_id: Option<String>,
    pub publish_at: Option<String>,
    pub uploaded_at: Option<String>,
    pub processing_status: Option<String>,
    pub thumbnail_pending: bool,
    pub repair: Option<Value>,
}

pub fn parse_id(clip_id: &str) -> Result<(String, i64)> {
    let (source, number) = clip_id
        .split_once(':')
        .with_context(|| format!("No queued clip with id {clip_id:?}; call list_queue."))?;
    let number = number
        .parse()
        .with_context(|| format!("No queued clip with id {clip_id:?}; call list_queue."))?;
    Ok((source.to_owned(), number))
}

fn json_column<T: serde::de::DeserializeOwned>(text: Option<String>) -> Option<T> {
    text.and_then(|t| serde_json::from_str(&t).ok())
}

impl Clip {
    pub fn id(&self) -> String {
        format!("{}:{:02}", self.source_id, self.number)
    }

    pub fn dir(&self, app: &App) -> PathBuf {
        app.clip_dir(&self.channel, &self.source_id, self.number)
    }

    pub fn video_path(&self, app: &App) -> PathBuf {
        self.dir(app).join("video.mp4")
    }

    pub fn thumbnail_path(&self, app: &App) -> PathBuf {
        self.dir(app).join("thumbnail.jpg")
    }

    pub fn youtube_url(&self) -> Option<String> {
        self.youtube_id
            .as_ref()
            .map(|id| format!("https://youtube.com/watch?v={id}"))
    }

    /// What list_queue shows the agent.
    pub fn view(&self) -> Value {
        json!({
            "id": self.id(),
            "status": self.status,
            "title": self.title,
            "speaker": self.speaker,
            "description": self.description,
            "tags": self.tags,
            "duration_minutes": (self.duration_ms as f64 / 6000.0).round() / 10.0,
            "predicted_score": self.score,
            "thumbnail_prompt": self.thumbnail_prompt,
            "chapters": self.chapters,
            "source_url": watch_url(&self.source_id),
            "youtube_url": self.youtube_url(),
            "publish_at": self.publish_at,
            "discard_reason": self.discard_reason,
            "thumbnail_pending": self.thumbnail_pending,
            "processing_status": self.processing_status,
            "repair": self.repair,
            "source": self.source,
        })
    }

    fn from_row(row: &rusqlite::Row) -> rusqlite::Result<Self> {
        Ok(Self {
            channel: row.get("channel")?,
            source_id: row.get("source_id")?,
            number: row.get("number")?,
            status: row.get("status")?,
            title: row.get("title")?,
            description: row.get("description")?,
            speaker: row.get("speaker")?,
            tags: json_column(row.get("tags")?).unwrap_or_default(),
            thumbnail_prompt: row.get("thumbnail_prompt")?,
            chapters: json_column(row.get("chapters")?).unwrap_or_default(),
            score: row.get("score")?,
            start_ms: row.get("start_ms")?,
            end_ms: row.get("end_ms")?,
            duration_ms: row.get("duration_ms")?,
            source: json_column(row.get("source")?),
            edit_report: json_column(row.get("edit_report")?),
            created_at: row.get("created_at")?,
            discard_reason: row.get("discard_reason")?,
            youtube_id: row.get("youtube_id")?,
            publish_at: row.get("publish_at")?,
            uploaded_at: row.get("uploaded_at")?,
            processing_status: row.get("processing_status")?,
            thumbnail_pending: row.get("thumbnail_pending")?,
            repair: json_column(row.get("repair")?),
        })
    }

    /// Insert or replace the whole row.
    pub fn save(&self, c: &Connection) -> Result<()> {
        c.execute(
            "INSERT OR REPLACE INTO clips (channel, source_id, number, status, title, description, speaker,
                tags, thumbnail_prompt, chapters, score, start_ms, end_ms, duration_ms, source, edit_report,
                created_at, discard_reason, youtube_id, publish_at, uploaded_at, processing_status,
                thumbnail_pending, repair)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20,
                ?21, ?22, ?23, ?24)",
            params![
                self.channel,
                self.source_id,
                self.number,
                self.status,
                self.title,
                self.description,
                self.speaker,
                to_json(&self.tags),
                self.thumbnail_prompt,
                to_json(&self.chapters),
                self.score,
                self.start_ms,
                self.end_ms,
                self.duration_ms,
                self.source.as_ref().map(to_json),
                self.edit_report.as_ref().map(|r| r.to_string()),
                self.created_at,
                self.discard_reason,
                self.youtube_id,
                self.publish_at,
                self.uploaded_at,
                self.processing_status,
                self.thumbnail_pending,
                self.repair.as_ref().map(|r| r.to_string()),
            ],
        )?;
        Ok(())
    }
}

fn to_json<T: serde::Serialize>(value: &T) -> String {
    serde_json::to_string(value).unwrap_or_default()
}

pub fn get(c: &Connection, channel: &str, clip_id: &str) -> Result<Clip> {
    let (source, number) = parse_id(clip_id)?;
    c.query_row(
        "SELECT * FROM clips WHERE channel = ?1 AND source_id = ?2 AND number = ?3",
        params![channel, source, number],
        Clip::from_row,
    )
    .optional()?
    .with_context(|| format!("No queued clip with id {clip_id:?}; call list_queue."))
}

pub fn pending(c: &Connection, channel: &str, clip_id: &str) -> Result<Clip> {
    let clip = get(c, channel, clip_id)?;
    if clip.status != "pending" {
        bail!("Clip is {}, not pending.", clip.status);
    }
    Ok(clip)
}

/// `get` from async code.
pub async fn load(app: &App, channel: &str, clip_id: &str) -> Result<Clip> {
    let (channel, id) = (channel.to_owned(), clip_id.to_owned());
    app.db.call(move |c| get(c, &channel, &id)).await
}

/// `pending` from async code.
pub async fn load_pending(app: &App, channel: &str, clip_id: &str) -> Result<Clip> {
    let (channel, id) = (channel.to_owned(), clip_id.to_owned());
    app.db.call(move |c| pending(c, &channel, &id)).await
}

/// `Clip::save` from async code.
pub async fn store(app: &App, clip: &Clip) -> Result<()> {
    let clip = clip.clone();
    app.db.call(move |c| clip.save(c)).await
}

/// The channel's clips in queue order; `status` "all" lists every one.
pub fn list(c: &Connection, channel: &str, status: &str) -> Result<Vec<Clip>> {
    let mut statement = c.prepare(
        "SELECT * FROM clips WHERE channel = ?1 AND (?2 = 'all' OR status = ?2)
         ORDER BY created_at, source_id, number",
    )?;
    let clips = statement
        .query_map(params![channel, status], Clip::from_row)?
        .collect::<rusqlite::Result<_>>()?;
    Ok(clips)
}

/// The clip uploaded as `video_id`, or whose repair replaced that video.
pub fn by_video(c: &Connection, channel: &str, video_id: &str) -> Result<Option<Clip>> {
    Ok(c.query_row(
        "SELECT * FROM clips WHERE channel = ?1
           AND (youtube_id = ?2 OR json_extract(repair, '$.original_id') = ?2)",
        params![channel, video_id],
        Clip::from_row,
    )
    .optional()?)
}
