//! Hourly Data API counter snapshots and Studio readings, kept raw and dated
//! so the agent chooses its own comparisons.

use std::sync::Arc;

use anyhow::{Result, bail};
use rusqlite::params;
use serde_json::{Map, Value, json};

use crate::app::{App, now, parse_time};
use crate::youtube;

const RETENTION_DAYS: i64 = 35;
static SNAPSHOT: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn cutoff() -> String {
    (now() - jiff::SignedDuration::from_hours(24 * RETENTION_DAYS)).to_string()
}

/// Take a counter snapshot unless one is under ten minutes old.
pub async fn snapshot(app: &App, channel: &str) -> Result<()> {
    let _guard = SNAPSHOT.lock().await;
    let name = channel.to_owned();
    let latest: Option<String> = app
        .db
        .call(move |c| {
            Ok(c.query_row(
                "SELECT max(observed_at) FROM counter_snapshots WHERE channel = ?1",
                [&name],
                |r| r.get(0),
            )?)
        })
        .await?;
    if let Some(latest) = latest
        && now().duration_since(parse_time(&latest)?) < jiff::SignedDuration::from_mins(10)
    {
        return Ok(());
    }
    let videos = youtube::channel_videos(app, channel, true).await?;
    let statistics = youtube::mine(app, channel, "statistics").await?;
    let subscribers: Option<i64> = statistics["statistics"]["subscriberCount"]
        .as_str()
        .and_then(|s| s.parse().ok());
    let (name, observed, cutoff) = (channel.to_owned(), now().to_string(), cutoff());
    app.db
        .call(move |c| {
            let transaction = c.transaction()?;
            transaction.execute(
                "INSERT INTO counter_snapshots (channel, observed_at, subscribers) VALUES (?1, ?2, ?3)",
                params![name, observed, subscribers],
            )?;
            for v in videos.iter() {
                transaction.execute(
                    "INSERT INTO counter_videos (channel, observed_at, video_id, title, privacy, publish_time, views, likes, comments)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                    params![name, observed, v.id, v.title, v.privacy, v.publish_time, v.views, v.likes, v.comments],
                )?;
            }
            transaction.execute("DELETE FROM counter_snapshots WHERE channel = ?1 AND observed_at < ?2", params![name, cutoff])?;
            transaction.commit()?;
            Ok(())
        })
        .await
}

/// Raw snapshots, newest first, plus the latest Studio readings.
pub async fn observations(app: &App, channel: &str, offset: usize, limit: usize) -> Result<Value> {
    if !(1..=200).contains(&limit) {
        bail!("offset must be nonnegative and limit between 1 and 200.");
    }
    snapshot(app, channel).await?;
    let name = channel.to_owned();
    let (snapshots, count) = app
        .db
        .call(move |c| {
            let count: i64 = c.query_row("SELECT count(*) FROM counter_snapshots WHERE channel = ?1", [&name], |r| r.get(0))?;
            let mut statement = c.prepare(
                "SELECT observed_at, subscribers FROM counter_snapshots WHERE channel = ?1
                 ORDER BY observed_at DESC LIMIT ?2 OFFSET ?3",
            )?;
            let rows: Vec<(String, Option<i64>)> = statement
                .query_map(params![name, limit as i64, offset as i64], |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?;
            let mut videos = c.prepare(
                "SELECT video_id, views, likes, comments, title, privacy, publish_time FROM counter_videos
                 WHERE channel = ?1 AND observed_at = ?2 ORDER BY publish_time DESC",
            )?;
            let mut snapshots = Vec::new();
            for (observed, subscribers) in rows {
                let mut map = Map::new();
                let mut rows = videos.query(params![name, observed])?;
                while let Some(r) = rows.next()? {
                    map.insert(
                        r.get(0)?,
                        json!({
                            "views": r.get::<_, i64>(1)?,
                            "likes": r.get::<_, i64>(2)?,
                            "comments": r.get::<_, i64>(3)?,
                            "title": r.get::<_, String>(4)?,
                            "privacy": r.get::<_, String>(5)?,
                            "publish_time": r.get::<_, String>(6)?,
                        }),
                    );
                }
                snapshots.push(json!({ "observed_at": observed, "subscribers": subscribers, "videos": map }));
            }
            Ok((snapshots, count as usize))
        })
        .await?;
    let stop = (offset + limit).min(count);
    Ok(json!({
        "snapshots": snapshots,
        "snapshot_count": count,
        "next_offset": if stop < count { Some(stop) } else { None },
        "studio": studio(app, channel).await?,
    }))
}

fn age_hours(observed: &str) -> f64 {
    parse_time(observed).map_or(f64::INFINITY, |t| {
        now().duration_since(t).as_secs_f64() / 3600.0
    })
}

/// The latest Studio reading for each video and for the channel.
pub async fn studio(app: &App, channel: &str) -> Result<Value> {
    let name = channel.to_owned();
    app.db
        .call(move |c| {
            let mut statement = c.prepare(
                "SELECT video_id, observed_at, url, period, approximate, currency, metrics, source
                 FROM studio_video_readings r WHERE channel = ?1 AND observed_at =
                   (SELECT max(observed_at) FROM studio_video_readings WHERE channel = r.channel AND video_id = r.video_id)
                 ORDER BY observed_at",
            )?;
            let videos: Vec<Value> = statement
                .query_map([&name], |r| {
                    let observed: String = r.get(1)?;
                    let age = age_hours(&observed);
                    Ok(json!({
                        "video_id": r.get::<_, String>(0)?,
                        "observed_at": observed,
                        "url": r.get::<_, String>(2)?,
                        "period": r.get::<_, String>(3)?,
                        "approximate": r.get::<_, bool>(4)?,
                        "currency": r.get::<_, String>(5)?,
                        "metrics": serde_json::from_str::<Value>(&r.get::<_, String>(6)?).unwrap_or_default(),
                        "source": r.get::<_, String>(7)?,
                        "age_hours": (age * 100.0).round() / 100.0,
                        "stale": age > 26.0,
                    }))
                })?
                .collect::<rusqlite::Result<_>>()?;
            let channel_reading = channel_reading(c, &name)?.map(|mut reading| {
                let age = age_hours(reading["observed_at"].as_str().unwrap_or(""));
                reading["age_hours"] = json!((age * 100.0).round() / 100.0);
                reading["stale"] = json!(age > 2.0);
                reading
            });
            let collector = c
                .query_row(
                    "SELECT checked_at, saved, errors FROM studio_status WHERE channel = ?1",
                    [&name],
                    |r| {
                        Ok(json!({
                            "checked_at": r.get::<_, String>(0)?,
                            "saved": r.get::<_, i64>(1)?,
                            "errors": serde_json::from_str::<Value>(&r.get::<_, String>(2)?).unwrap_or_default(),
                        }))
                    },
                )
                .unwrap_or_else(|_| json!({"status": "not collected yet"}));
            Ok(json!({
                "channel": channel_reading,
                "collector": collector,
                "source": "YouTube Studio analytics",
                "note": "Since-published readings, separate from public API windows. Each reading identifies its source and whether it is approximate. Stale readings are not fresh experiment evidence. Missing metrics stay unknown.",
                "videos": videos,
            }))
        })
        .await
}

/// The channel's latest Studio daily series, as stored.
pub fn channel_reading(c: &rusqlite::Connection, channel: &str) -> Result<Option<Value>> {
    use rusqlite::OptionalExtension;
    Ok(c.query_row(
        "SELECT channel_id, monetized, observed_at, source, currency, reporting_timezone, start_date,
                end_date_exclusive, approximate, daily FROM studio_channel_readings WHERE channel = ?1",
        [channel],
        |r| {
            Ok(json!({
                "channel_id": r.get::<_, String>(0)?,
                "monetized": r.get::<_, bool>(1)?,
                "observed_at": r.get::<_, String>(2)?,
                "source": r.get::<_, String>(3)?,
                "currency": r.get::<_, String>(4)?,
                "reporting_timezone": r.get::<_, String>(5)?,
                "start_date": r.get::<_, String>(6)?,
                "end_date_exclusive": r.get::<_, String>(7)?,
                "approximate": r.get::<_, bool>(8)?,
                "daily": serde_json::from_str::<Value>(&r.get::<_, String>(9)?).unwrap_or_default(),
            }))
        },
    )
    .optional()?)
}

/// Every hour: counters for each channel, then queue state from YouTube.
pub fn start_sampler(app: Arc<App>) {
    tokio::spawn(async move {
        loop {
            for channel in app.channel_names().unwrap_or_default() {
                let result = async {
                    snapshot(&app, &channel).await?;
                    let videos = youtube::channel_videos(&app, &channel, false).await?;
                    crate::publish::reconcile(&app, &channel, &videos).await
                }
                .await;
                if let Err(error) = result {
                    tracing::warn!("Counter snapshot failed for {channel}: {error:#}");
                }
            }
            tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
        }
    });
}
