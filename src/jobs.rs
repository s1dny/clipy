//! Every channel's long work, run in the background and persisted, so a
//! restart resumes or fails each job explicitly. Media work and YouTube work
//! are separate lanes, each one job at a time, so an upload never waits
//! behind hours of rendering.

use std::sync::Arc;

use anyhow::{Result, bail};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

use crate::app::{App, now};
use crate::db::Db;

const LOG_LINES: usize = 40;

/// Kinds safe to restart from the top after the server dies mid-run. Each
/// step is cached; an interrupted upload may already exist on YouTube, so
/// publishes and repairs are failed instead of retried.
const RESUMABLE: [&str; 2] = ["prepare_source", "process_source"];

/// The lanes: downloading, transcribing and rendering; then uploads and
/// changes to YouTube.
const LANES: [&[&str]; 2] = [
    &["prepare_source", "process_source", "edit_video"],
    &["publish", "thumbnails", "repair"],
];

/// A job's log: the last lines are kept on the job for the agent to read.
#[derive(Clone)]
pub struct JobLog {
    job: Option<(Db, String)>,
}

impl JobLog {
    /// Log to the console only, for the command-line tools.
    pub fn console() -> Self {
        Self { job: None }
    }

    pub fn info(&self, message: impl Into<String>) {
        let message = message.into();
        tracing::info!("{message}");
        let Some((db, id)) = &self.job else { return };
        let line = format!("{} {message}", now().strftime("%H:%M:%S"));
        let id = id.clone();
        let _ = db.with(|c| {
            let log: String =
                c.query_row("SELECT log FROM jobs WHERE id = ?1", [&id], |r| r.get(0))?;
            let mut lines: Vec<String> = serde_json::from_str(&log).unwrap_or_default();
            lines.push(line);
            let excess = lines.len().saturating_sub(LOG_LINES);
            lines.drain(..excess);
            c.execute(
                "UPDATE jobs SET log = ?2 WHERE id = ?1",
                params![id, serde_json::to_string(&lines)?],
            )?;
            Ok(())
        });
    }
}

pub struct Jobs {
    app: Arc<App>,
    wake: [tokio::sync::Notify; 2],
}

fn row_to_json(row: &rusqlite::Row) -> rusqlite::Result<Value> {
    let parse = |text: Option<String>| {
        text.and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .unwrap_or(Value::Null)
    };
    Ok(json!({
        "id": row.get::<_, String>("id")?,
        "channel": row.get::<_, String>("channel")?,
        "kind": row.get::<_, String>("kind")?,
        "args": parse(row.get("args")?),
        "status": row.get::<_, String>("status")?,
        "submitted_at": row.get::<_, String>("submitted_at")?,
        "started_at": row.get::<_, Option<String>>("started_at")?,
        "finished_at": row.get::<_, Option<String>>("finished_at")?,
        "result": parse(row.get("result")?),
        "error": row.get::<_, Option<String>>("error")?,
        "log": parse(row.get("log")?),
    }))
}

impl Jobs {
    /// Recover jobs a previous server left running, then start the worker.
    pub fn start(app: Arc<App>) -> Result<Arc<Self>> {
        app.db.with(|c| {
            c.execute(
                &format!(
                    "UPDATE jobs SET status = 'queued', started_at = NULL
                     WHERE status = 'running' AND kind IN ({})",
                    RESUMABLE.map(|k| format!("'{k}'")).join(",")
                ),
                [],
            )?;
            c.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?1,
                     error = 'Server restarted mid-job; check YouTube before retrying.'
                 WHERE status = 'running'",
                [now().to_string()],
            )?;
            Ok(())
        })?;
        let jobs = Arc::new(Self {
            app,
            wake: Default::default(),
        });
        for lane in 0..LANES.len() {
            tokio::spawn(jobs.clone().work(lane));
        }
        Ok(jobs)
    }

    pub async fn submit(&self, channel: &str, kind: &str, args: Value) -> Result<Value> {
        let id = uuid::Uuid::new_v4().simple().to_string()[..8].to_owned();
        let (channel, kind, job_id) = (channel.to_owned(), kind.to_owned(), id.clone());
        self.app
            .db
            .call(move |c| {
                c.execute(
                    "INSERT INTO jobs (id, channel, kind, args, status, submitted_at)
                     VALUES (?1, ?2, ?3, ?4, 'queued', ?5)",
                    params![job_id, channel, kind, args.to_string(), now().to_string()],
                )?;
                Ok(())
            })
            .await?;
        for wake in &self.wake {
            wake.notify_one();
        }
        self.get(&id).await
    }

    pub async fn get(&self, id: &str) -> Result<Value> {
        let id = id.to_owned();
        self.app
            .db
            .call(move |c| {
                c.query_row("SELECT * FROM jobs WHERE id = ?1", [&id], row_to_json)
                    .optional()?
                    .ok_or_else(|| anyhow::anyhow!("No job {id:?}; see list_jobs."))
            })
            .await
    }

    /// The channel's newest jobs first.
    pub async fn list(&self, channel: &str, limit: usize) -> Result<Vec<Value>> {
        let channel = channel.to_owned();
        self.app
            .db
            .call(move |c| {
                let mut statement = c.prepare(
                    "SELECT * FROM jobs WHERE channel = ?1 ORDER BY submitted_at DESC, rowid DESC LIMIT ?2",
                )?;
                let rows = statement
                    .query_map(params![channel, limit as i64], row_to_json)?
                    .collect::<rusqlite::Result<_>>()?;
                Ok(rows)
            })
            .await
    }

    /// The channel's queued or running jobs of a kind.
    pub async fn active(&self, channel: &str, kind: &str) -> Result<Vec<Value>> {
        let (channel, kind) = (channel.to_owned(), kind.to_owned());
        self.app
            .db
            .call(move |c| {
                let mut statement = c.prepare(
                    "SELECT * FROM jobs WHERE channel = ?1 AND kind = ?2
                     AND status IN ('queued', 'running') ORDER BY submitted_at",
                )?;
                let rows = statement
                    .query_map(params![channel, kind], row_to_json)?
                    .collect::<rusqlite::Result<_>>()?;
                Ok(rows)
            })
            .await
    }

    async fn next(&self, lane: usize) -> Result<Option<(String, String, String, Value)>> {
        let kinds = LANES[lane]
            .iter()
            .map(|k| format!("'{k}'"))
            .collect::<Vec<_>>()
            .join(",");
        self.app
            .db
            .call(move |c| {
                Ok(c.query_row(
                    &format!(
                        "SELECT id, channel, kind, args FROM jobs
                         WHERE status = 'queued' AND kind IN ({kinds})
                         ORDER BY submitted_at, rowid LIMIT 1"
                    ),
                    [],
                    |r| {
                        Ok((
                            r.get(0)?,
                            r.get(1)?,
                            r.get(2)?,
                            serde_json::from_str(&r.get::<_, String>(3)?).unwrap_or(Value::Null),
                        ))
                    },
                )
                .optional()?)
            })
            .await
    }

    async fn work(self: Arc<Self>, lane: usize) {
        loop {
            let next = match self.next(lane).await {
                Ok(next) => next,
                Err(error) => {
                    tracing::error!("Job queue unreadable: {error:#}");
                    None
                }
            };
            let Some((id, channel, kind, args)) = next else {
                let _ = tokio::time::timeout(
                    std::time::Duration::from_secs(30),
                    self.wake[lane].notified(),
                )
                .await;
                continue;
            };
            let started = id.clone();
            let _ = self
                .app
                .db
                .call(move |c| {
                    c.execute(
                        "UPDATE jobs SET status = 'running', started_at = ?2 WHERE id = ?1",
                        params![started, now().to_string()],
                    )?;
                    Ok(())
                })
                .await;
            let log = JobLog {
                job: Some((self.app.db.clone(), id.clone())),
            };
            // A job failure, even a panic, must not kill the worker.
            let app = self.app.clone();
            let outcome =
                tokio::spawn(async move { run(&app, &channel, &kind, args, &log).await }).await;
            let (status, result, error) = match outcome {
                Ok(Ok(result)) => ("succeeded", Some(result.to_string()), None),
                Ok(Err(error)) => {
                    tracing::error!("Job {id} failed: {error:#}");
                    ("failed", None, Some(format!("{error:#}")))
                }
                Err(panic) => ("failed", None, Some(format!("Job crashed: {panic}"))),
            };
            let _ = self
                .app
                .db
                .call(move |c| {
                    c.execute(
                        "UPDATE jobs SET status = ?2, result = ?3, error = ?4, finished_at = ?5 WHERE id = ?1",
                        params![id, status, result, error, now().to_string()],
                    )?;
                    Ok(())
                })
                .await;
        }
    }
}

async fn run(app: &App, channel: &str, kind: &str, args: Value, log: &JobLog) -> Result<Value> {
    let text = |key: &str| args[key].as_str().map(str::to_owned);
    match kind {
        "prepare_source" => {
            let url = text("url").unwrap_or_default();
            crate::pipeline::prepare(
                app,
                &url,
                args["include_video"].as_bool().unwrap_or(false),
                log,
            )
            .await
        }
        "process_source" => {
            let selections = match args.get("clips") {
                Some(Value::Null) | None => None,
                Some(clips) => Some(serde_json::from_value(clips.clone())?),
            };
            let speaker = text("speaker").unwrap_or_default();
            let made = crate::pipeline::process(
                app,
                channel,
                &text("url").unwrap_or_default(),
                &speaker,
                selections,
                log,
            )
            .await?;
            Ok(format!("{made} clips of {speaker} are in the queue.").into())
        }
        "edit_video" => {
            crate::pipeline::edit_pending(
                app,
                channel,
                &text("clip_id").unwrap_or_default(),
                &args["changes"],
                log,
            )
            .await
        }
        "publish" => {
            crate::publish::publish(
                app,
                channel,
                &text("clip_id").unwrap_or_default(),
                text("publish_at"),
                log,
            )
            .await
        }
        "thumbnails" => crate::publish::retry_thumbnails(app, channel).await,
        "repair" => {
            crate::recovery::repair(
                app,
                channel,
                &text("video_id").unwrap_or_default(),
                text("publish_at"),
                log,
            )
            .await
        }
        _ => bail!("Unknown job kind {kind}"),
    }
}
