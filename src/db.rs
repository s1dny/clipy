//! Everything Clipy remembers, in one SQLite database at <data>/clipy.db.
//! Media and credentials stay as files; every other piece of state is a row.

use std::path::Path;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result};
use rusqlite::Connection;

const SCHEMA: &str = r#"
CREATE TABLE notes (
    channel TEXT PRIMARY KEY,
    content TEXT NOT NULL
);

-- The agent's replacements for the prompt templates in config.toml.
CREATE TABLE prompt_overrides (
    channel TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (channel, key)
);

-- Speaker-labelled sentences of a source video, shared by every channel.
CREATE TABLE transcripts (
    video_id TEXT PRIMARY KEY,
    sentences TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- The agent's clip choices for a source. Immutable once saved, apart from
-- edit_video revising a pending clip's edits.
CREATE TABLE selections (
    channel TEXT NOT NULL,
    source_id TEXT NOT NULL,
    speaker TEXT NOT NULL,
    clips TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, source_id)
);

-- The upload queue: one row per rendered clip.
CREATE TABLE clips (
    channel TEXT NOT NULL,
    source_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'uploaded', 'failed', 'discarded')),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    speaker TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    thumbnail_prompt TEXT,
    chapters TEXT NOT NULL DEFAULT '[]',
    score REAL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    source TEXT,
    edit_report TEXT,
    created_at TEXT NOT NULL,
    discard_reason TEXT,
    youtube_id TEXT,
    publish_at TEXT,
    uploaded_at TEXT,
    processing_status TEXT,
    thumbnail_pending INTEGER NOT NULL DEFAULT 0,
    repair TEXT,
    PRIMARY KEY (channel, source_id, number)
);
CREATE INDEX clips_youtube_id ON clips (youtube_id);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    submitted_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result TEXT,
    error TEXT,
    log TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX jobs_channel ON jobs (channel, submitted_at);

-- Google API mutations, recorded before they are sent.
CREATE TABLE api_writes (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    api TEXT NOT NULL,
    version TEXT NOT NULL,
    method TEXT NOT NULL,
    parameters TEXT NOT NULL,
    media_path TEXT,
    observation TEXT,
    status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'unconfirmed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    response TEXT,
    error TEXT
);

-- Studio requests: the journal and the queue the private browser's worker drains.
CREATE TABLE studio_requests (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    payload TEXT NOT NULL,
    observation TEXT,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'responded', 'unconfirmed')),
    submitted_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    response TEXT,
    error TEXT
);

-- Hourly YouTube Data API counters.
CREATE TABLE counter_snapshots (
    channel TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    subscribers INTEGER,
    PRIMARY KEY (channel, observed_at)
);
CREATE TABLE counter_videos (
    channel TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    privacy TEXT NOT NULL,
    publish_time TEXT NOT NULL,
    views INTEGER NOT NULL,
    likes INTEGER NOT NULL,
    comments INTEGER NOT NULL,
    PRIMARY KEY (channel, observed_at, video_id),
    FOREIGN KEY (channel, observed_at) REFERENCES counter_snapshots ON DELETE CASCADE
);

-- Studio analytics: since-published readings per video.
CREATE TABLE studio_video_readings (
    channel TEXT NOT NULL,
    video_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    url TEXT NOT NULL,
    period TEXT NOT NULL,
    approximate INTEGER NOT NULL,
    currency TEXT NOT NULL,
    metrics TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (channel, video_id, observed_at)
);

-- The latest daily channel series from Studio.
CREATE TABLE studio_channel_readings (
    channel TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    monetized INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    currency TEXT NOT NULL,
    reporting_timezone TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date_exclusive TEXT NOT NULL,
    approximate INTEGER NOT NULL,
    daily TEXT NOT NULL
);

CREATE TABLE studio_status (
    channel TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    saved INTEGER NOT NULL,
    errors TEXT NOT NULL
);
"#;

/// One connection, shared; SQLite serialises writers anyway, and WAL lets the
/// Studio collector's process read and write the same file alongside the server.
#[derive(Clone)]
pub struct Db(Arc<Mutex<Connection>>);

impl Db {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut connection =
            Connection::open(path).with_context(|| format!("open {}", path.display()))?;
        connection.busy_timeout(std::time::Duration::from_secs(30))?;
        connection.pragma_update(None, "journal_mode", "WAL")?;
        connection.pragma_update(None, "synchronous", "NORMAL")?;
        connection.pragma_update(None, "foreign_keys", true)?;
        let version: i64 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
        if version == 0 {
            let transaction = connection.transaction()?;
            transaction.execute_batch(SCHEMA)?;
            transaction.pragma_update(None, "user_version", 1)?;
            transaction.commit()?;
        }
        Ok(Self(Arc::new(Mutex::new(connection))))
    }

    /// Run `f` on the connection, blocking the current thread.
    pub fn with<T>(&self, f: impl FnOnce(&mut Connection) -> Result<T>) -> Result<T> {
        let mut connection = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        f(&mut connection)
    }

    /// Run `f` on the connection from async code.
    pub async fn call<T: Send + 'static>(
        &self,
        f: impl FnOnce(&mut Connection) -> Result<T> + Send + 'static,
    ) -> Result<T> {
        let db = self.clone();
        tokio::task::spawn_blocking(move || db.with(f)).await?
    }
}

/// Run a read-only statement for the observer and return its rows as JSON
/// objects. Refuses anything that would change the database.
pub fn read_only_query(
    connection: &Connection,
    sql: &str,
    limit: usize,
) -> Result<serde_json::Value> {
    let mut statement = connection.prepare(sql)?;
    if !statement.readonly() {
        anyhow::bail!("Only read-only statements are allowed.");
    }
    let columns: Vec<String> = statement.column_names().map(|c| c.to_string()).collect();
    let mut rows = statement.query([])?;
    let mut out = Vec::new();
    let mut truncated = false;
    while let Some(row) = rows.next()? {
        if out.len() == limit {
            truncated = true;
            break;
        }
        let mut object = serde_json::Map::new();
        for (index, name) in columns.iter().enumerate() {
            use rusqlite::types::ValueRef;
            let value = match row.get_ref(index)? {
                ValueRef::Null => serde_json::Value::Null,
                ValueRef::Integer(n) => n.into(),
                ValueRef::Real(n) => n.into(),
                ValueRef::Text(text) => String::from_utf8_lossy(text).into_owned().into(),
                ValueRef::Blob(blob) => format!("<{} byte blob>", blob.len()).into(),
            };
            object.insert(name.clone(), value);
        }
        out.push(serde_json::Value::Object(object));
    }
    Ok(serde_json::json!({ "columns": columns, "rows": out, "truncated": truncated }))
}
