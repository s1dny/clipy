//! One-shot import of the previous file-based layout into the database and
//! the new media layout. Run once at cutover, then removed.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use rusqlite::params;
use serde_json::{Value, json};

use crate::app::App;
use crate::clips::ClipSelection;
use crate::queue::Clip;

fn read(path: &Path) -> Option<Value> {
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

fn text(value: &Value) -> Option<String> {
    value.as_str().map(str::to_owned)
}

fn json_text(value: &Value) -> Option<String> {
    (!value.is_null()).then(|| value.to_string())
}

/// Move a file or folder, creating the destination's parent.
fn relocate(from: &Path, to: &Path) -> Result<()> {
    if !from.exists() || to.exists() {
        return Ok(());
    }
    std::fs::create_dir_all(to.parent().context("no parent")?)?;
    std::fs::rename(from, to)
        .with_context(|| format!("move {} to {}", from.display(), to.display()))
}

pub fn run(app: &App) -> Result<()> {
    let config = app.config()?;
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    let mut problems = Vec::new();
    for channel in config.names() {
        let dir = app.channel_dir(&channel);
        let speaker = app.channel_config(&channel)?.speaker;

        if let Ok(notes) = std::fs::read_to_string(dir.join("notes.md")) {
            app.db.with(|c| {
                Ok(c.execute(
                    "INSERT OR REPLACE INTO notes VALUES (?1, ?2)",
                    params![channel, notes],
                )?)
            })?;
            *counts.entry("notes").or_default() += 1;
        }
        if let Some(Value::Object(overrides)) = read(&dir.join("prompt_overrides.json")) {
            for key in crate::config::PROMPT_KEYS {
                if let Some(value) = overrides.get(key).and_then(Value::as_str) {
                    app.db.with(|c| {
                        Ok(c.execute(
                            "INSERT OR REPLACE INTO prompt_overrides VALUES (?1, ?2, ?3)",
                            params![channel, key, value],
                        )?)
                    })?;
                    *counts.entry("prompt overrides").or_default() += 1;
                }
            }
        }
        if let Some(old) = read(&dir.join(".youtube_token.json")) {
            let token = crate::youtube::Token {
                client_id: text(&old["client_id"]).context("token client_id")?,
                client_secret: text(&old["client_secret"]).context("token client_secret")?,
                refresh_token: text(&old["refresh_token"]).context("token refresh_token")?,
                scopes: serde_json::from_value(old["scopes"].clone()).unwrap_or_default(),
                access_token: text(&old["token"]),
                expires_at: old["expiry"]
                    .as_str()
                    .and_then(|e| crate::app::parse_time(e).ok()),
            };
            crate::youtube::save_token(&app.token_file(&channel), &token)?;
            *counts.entry("tokens").or_default() += 1;
        }

        // Sources: transcripts, saved selections and any downloaded media.
        let mut sources: Vec<PathBuf> = std::fs::read_dir(&dir)?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| {
                p.is_dir()
                    && crate::app::is_video_id(&p.file_name().unwrap_or_default().to_string_lossy())
            })
            .collect();
        sources.sort();
        for source_dir in &sources {
            let source = source_dir
                .file_name()
                .unwrap()
                .to_string_lossy()
                .into_owned();
            let sentences = read(&source_dir.join("sentences.json")).or_else(|| {
                read(&source_dir.join("transcript.json"))
                    .map(|t| json!(crate::transcribe::sentences(&t)))
            });
            if let Some(sentences) = sentences {
                let inserted = app.db.with(|c| {
                    Ok(c.execute(
                        "INSERT OR IGNORE INTO transcripts (video_id, sentences, created_at) VALUES (?1, ?2, ?3)",
                        params![source, sentences.to_string(), crate::app::now().to_string()],
                    )?)
                })?;
                *counts.entry("transcripts").or_default() += inserted;
            }
            if let Some(Value::Array(plans)) = read(&source_dir.join("clips.json")) {
                let parsed: Result<Vec<ClipSelection>, _> = plans
                    .iter()
                    .map(|p| serde_json::from_value(p.clone()))
                    .collect();
                match parsed {
                    Ok(selections) => {
                        let plan_speaker = plans
                            .first()
                            .and_then(|p| text(&p["speaker"]))
                            .or(speaker.clone())
                            .unwrap_or_default();
                        app.db.with(|c| {
                            Ok(c.execute(
                                "INSERT OR REPLACE INTO selections VALUES (?1, ?2, ?3, ?4, ?5)",
                                params![
                                    channel,
                                    source,
                                    plan_speaker,
                                    serde_json::to_string(&selections)?,
                                    crate::app::now().to_string()
                                ],
                            )?)
                        })?;
                        *counts.entry("selections").or_default() += 1;
                    }
                    Err(error) => problems.push(format!("{channel}/{source}/clips.json: {error}")),
                }
            }
            for stem in ["audio", "video"] {
                if let Some(media) = crate::sources::downloaded(source_dir, stem) {
                    let target = app.source_dir(&source).join(media.file_name().unwrap());
                    relocate(&media, &target)?;
                    *counts.entry("source media").or_default() += 1;
                }
            }
        }

        // The upload queue, with each clip's folder moved into place.
        for item in read(&dir.join("upload_queue.json"))
            .and_then(|q| q.as_array().cloned())
            .unwrap_or_default()
        {
            let Some(clip_path) = text(&item["clip_path"]) else {
                problems.push(format!(
                    "{channel}: queue item without clip_path: {}",
                    item["title"]
                ));
                continue;
            };
            let old = PathBuf::from(&clip_path.replacen(
                "/data/",
                &format!("{}/", app.data.display()),
                1,
            ));
            let folder = old.parent().context("clip folder")?.to_owned();
            let parts: Vec<String> = old
                .components()
                .map(|c| c.as_os_str().to_string_lossy().into_owned())
                .collect();
            let at = parts
                .iter()
                .position(|p| p == "clips")
                .context("clip path without clips/")?;
            let source = parts[at - 1].clone();
            let number: i64 = parts[at + 1][..2].parse().context("clip number")?;
            let report = read(&folder.join("edit-report.json"));
            let duration_ms = report
                .as_ref()
                .and_then(|r| r["duration_ms"].as_i64())
                .unwrap_or_else(|| {
                    (item["duration_seconds"].as_f64().unwrap_or(0.0) * 1000.0).round() as i64
                });
            let status = match text(&item["status"]).as_deref() {
                Some(s @ ("pending" | "uploaded" | "failed" | "discarded")) => s.to_owned(),
                other => {
                    problems.push(format!(
                        "{channel}: unknown status {other:?}; kept as failed"
                    ));
                    "failed".into()
                }
            };
            let clip = Clip {
                channel: channel.clone(),
                source_id: source.clone(),
                number,
                status,
                title: text(&item["title"]).unwrap_or_default(),
                description: text(&item["description"]).unwrap_or_default(),
                speaker: text(&item["speaker"]),
                tags: serde_json::from_value(item["tags"].clone()).unwrap_or_default(),
                thumbnail_prompt: text(&item["thumbnail_prompt"]),
                chapters: serde_json::from_value(item["chapters"].clone()).unwrap_or_default(),
                score: item["score"].as_f64(),
                start_ms: item["start_ms"].as_i64().unwrap_or(0),
                end_ms: item["end_ms"].as_i64().unwrap_or(0),
                duration_ms,
                source: serde_json::from_value(item["source"].clone()).ok(),
                edit_report: report,
                created_at: text(&item["created_at"])
                    .unwrap_or_else(|| crate::app::now().to_string()),
                discard_reason: text(&item["discard_reason"]),
                youtube_id: text(&item["youtube_id"]),
                publish_at: text(&item["publish_at"]),
                uploaded_at: text(&item["uploaded_at"]),
                processing_status: text(&item["processing_status"]),
                thumbnail_pending: item["thumbnail_pending"].as_bool().unwrap_or(false),
                repair: (!item["repair"].is_null()).then(|| item["repair"].clone()),
            };
            let target = clip.dir(app);
            relocate(&folder, &target)?;
            let _ = std::fs::remove_file(target.join("edit-report.json"));
            let _ = std::fs::remove_file(target.join(".thumbnail_prompt"));
            app.db.with(|c| clip.save(c))?;
            *counts.entry("clips").or_default() += 1;
        }

        for (folder, table) in [
            ("api-writes", "api_writes"),
            ("studio-api", "studio_requests"),
        ] {
            let Ok(entries) = std::fs::read_dir(dir.join(folder)) else {
                continue;
            };
            for entry in entries.filter_map(|e| e.ok()) {
                let Some(audit) = read(&entry.path()) else {
                    continue;
                };
                let started = text(&audit["started_at"]).unwrap_or_default();
                let unfinished = audit["status"] == "started";
                let error = if unfinished {
                    Some("Recorded before the outcome was known; inspect remote state.".to_owned())
                } else {
                    text(&audit["error"])
                };
                if table == "api_writes" {
                    let status = if unfinished {
                        "unconfirmed"
                    } else {
                        audit["status"].as_str().unwrap_or("unconfirmed")
                    };
                    app.db.with(|c| {
                        Ok(c.execute(
                            "INSERT OR IGNORE INTO api_writes VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                            params![
                                text(&audit["id"]),
                                channel,
                                text(&audit["api"]),
                                text(&audit["version"]),
                                text(&audit["method"]),
                                audit["parameters"].to_string(),
                                text(&audit["media_path"]),
                                json_text(&audit["observation"]),
                                status,
                                started,
                                text(&audit["finished_at"]),
                                json_text(&audit["response"]),
                                error,
                            ],
                        )?)
                    })?;
                } else {
                    let status = if audit["status"] == "responded" {
                        "responded"
                    } else {
                        "unconfirmed"
                    };
                    let channel_id = read(&dir.join("studio-channel.json"))
                        .and_then(|r| text(&r["channel_id"]))
                        .unwrap_or_default();
                    app.db.with(|c| {
                        Ok(c.execute(
                            "INSERT OR IGNORE INTO studio_requests VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
                            params![
                                text(&audit["id"]),
                                channel,
                                channel_id,
                                text(&audit["endpoint"]),
                                audit["payload"].to_string(),
                                json_text(&audit["observation"]),
                                status,
                                started,
                                started,
                                text(&audit["finished_at"]),
                                json_text(&audit["response"]),
                                error,
                            ],
                        )?)
                    })?;
                }
                *counts
                    .entry(if table == "api_writes" {
                        "api writes"
                    } else {
                        "studio requests"
                    })
                    .or_default() += 1;
            }
        }

        if let Some(Value::Array(history)) = read(&dir.join("performance.json")) {
            let cutoff =
                (crate::app::now() - jiff::SignedDuration::from_hours(24 * 35)).to_string();
            app.db.with(|c| {
                let transaction = c.transaction()?;
                for snapshot in &history {
                    let observed = crate::app::parse_time(snapshot["observed_at"].as_str().unwrap_or(""))?.to_string();
                    if observed < cutoff {
                        continue;
                    }
                    transaction.execute(
                        "INSERT OR IGNORE INTO counter_snapshots VALUES (?1, ?2, ?3)",
                        params![channel, observed, snapshot["subscribers"].as_i64()],
                    )?;
                    for (id, v) in snapshot["videos"].as_object().into_iter().flatten() {
                        transaction.execute(
                            "INSERT OR IGNORE INTO counter_videos VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                            params![
                                channel,
                                observed,
                                id,
                                text(&v["title"]).unwrap_or_default(),
                                text(&v["privacy"]).unwrap_or_default(),
                                text(&v["publish_time"]).unwrap_or_default(),
                                v["views"].as_i64().unwrap_or(0),
                                v["likes"].as_i64().unwrap_or(0),
                                v["comments"].as_i64().unwrap_or(0),
                            ],
                        )?;
                    }
                }
                transaction.commit()?;
                Ok(())
            })?;
            *counts.entry("counter snapshots").or_default() += history.len();
        }
        if let Some(Value::Array(readings)) = read(&dir.join("studio.json")) {
            app.db.with(|c| {
                let transaction = c.transaction()?;
                for r in &readings {
                    let observed = crate::app::parse_time(r["observed_at"].as_str().unwrap_or(""))?.to_string();
                    transaction.execute(
                        "INSERT OR IGNORE INTO studio_video_readings VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                        params![
                            channel,
                            text(&r["video_id"]),
                            observed,
                            text(&r["url"]),
                            text(&r["period"]).unwrap_or_else(|| "since_published".into()),
                            r["approximate"].as_bool().unwrap_or(true),
                            text(&r["currency"]).unwrap_or_else(|| "USD".into()),
                            r["metrics"].to_string(),
                            text(&r["source"]).unwrap_or_default(),
                        ],
                    )?;
                }
                transaction.commit()?;
                Ok(())
            })?;
            *counts.entry("studio readings").or_default() += readings.len();
        }
        if let Some(r) = read(&dir.join("studio-channel.json")) {
            app.db.with(|c| {
                Ok(c.execute(
                    "INSERT OR REPLACE INTO studio_channel_readings VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                    params![
                        channel,
                        text(&r["channel_id"]),
                        r["monetized"].as_bool().unwrap_or(false),
                        crate::app::parse_time(r["observed_at"].as_str().unwrap_or(""))?.to_string(),
                        text(&r["source"]),
                        text(&r["currency"]),
                        text(&r["reporting_timezone"]),
                        text(&r["start_date"]),
                        text(&r["end_date_exclusive"]),
                        r["approximate"].as_bool().unwrap_or(true),
                        r["daily"].to_string(),
                    ],
                )?)
            })?;
            *counts.entry("studio channel series").or_default() += 1;
        }
        if let Some(s) = read(&dir.join("studio-status.json")) {
            app.db.with(|c| {
                Ok(c.execute(
                    "INSERT OR REPLACE INTO studio_status VALUES (?1, ?2, ?3, ?4)",
                    params![
                        channel,
                        text(&s["checked_at"]),
                        s["saved"].as_i64().unwrap_or(0),
                        s["errors"].to_string()
                    ],
                )?)
            })?;
        }
    }

    for job in read(&app.data.join("jobs.json"))
        .and_then(|j| j.as_array().cloned())
        .unwrap_or_default()
    {
        let mut args = job["args"].clone();
        if let Some(item) = args.as_object_mut().and_then(|a| a.remove("item_id")) {
            args["clip_id"] = item;
        }
        // A job the old server never finished is not resumed by the new one.
        let status = job["status"].as_str().unwrap_or("failed");
        let error = if ["queued", "running"].contains(&job["status"].as_str().unwrap_or("")) {
            Some("Interrupted by the move to the new server; resubmit it.".to_owned())
        } else {
            text(&job["error"])
        };
        app.db.with(|c| {
            Ok(c.execute(
                "INSERT OR IGNORE INTO jobs VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                params![
                    text(&job["id"]),
                    text(&job["channel"]),
                    text(&job["kind"]),
                    args.to_string(),
                    status,
                    text(&job["submitted_at"]),
                    text(&job["started_at"]),
                    text(&job["finished_at"]),
                    json_text(&job["result"]),
                    error,
                    job.get("log").cloned().unwrap_or(json!([])).to_string(),
                ],
            )?)
        })?;
        *counts.entry("jobs").or_default() += 1;
    }

    for (what, n) in &counts {
        println!("{what}: {n}");
    }
    for problem in &problems {
        println!("problem: {problem}");
    }
    Ok(())
}
