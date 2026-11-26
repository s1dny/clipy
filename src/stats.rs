//! The daily report's stats header, from fresh channel-level Studio series.

use std::collections::BTreeMap;

use anyhow::{Result, bail};
use jiff::civil::Date;
use serde_json::Value;

use crate::app::{App, now, parse_time, thousands};

const WINDOW: i64 = 30;

type Daily = BTreeMap<String, BTreeMap<&'static str, Option<f64>>>;

fn daily(app: &App, channel: &str) -> Result<(Daily, bool)> {
    if !app.token_file(channel).exists() {
        bail!("reconnect YouTube with clipy auth {channel}");
    }
    let reading = app
        .db
        .with(|c| crate::observations::channel_reading(c, channel))?
        .ok_or_else(|| anyhow::anyhow!("channel totals not collected yet"))?;
    let observed = parse_time(reading["observed_at"].as_str().unwrap_or(""))?;
    let age = now().duration_since(observed);
    if age.is_negative() || age > jiff::SignedDuration::from_hours(2) {
        bail!("Studio channel totals are over two hours old");
    }
    if reading["currency"] != "AUD" {
        bail!("Studio totals need a fresh AUD collection");
    }
    let mut out = Daily::new();
    for (day, metrics) in reading["daily"].as_object().into_iter().flatten() {
        let get = |key: &str| metrics.get(key).and_then(Value::as_f64);
        out.insert(
            day.clone(),
            BTreeMap::from([
                ("views", get("views")),
                ("subscribers", get("subscribers")),
                ("earnings", get("revenue_aud")),
            ]),
        );
    }
    Ok((out, reading["monetized"] == false))
}

fn total(daily: &Daily, metric: &str, first: Date, last: Date) -> f64 {
    daily
        .iter()
        .filter(|(day, _)| (first.to_string()..=last.to_string()).contains(*day))
        .filter_map(|(_, values)| values.get(metric).copied().flatten())
        .sum()
}

fn money(n: f64) -> String {
    let cents = (n * 100.0).round() as i64;
    format!("A${}.{:02}", thousands(cents / 100), (cents % 100).abs())
}

fn compact(n: f64) -> String {
    let n = n.round();
    if n.abs() >= 1_000_000.0 {
        format!("{:.2}M", n / 1_000_000.0)
    } else if n.abs() >= 100_000.0 {
        format!("{:.0}K", n / 1_000.0)
    } else {
        thousands(n as i64)
    }
}

fn signed(n: f64) -> String {
    let n = n.round() as i64;
    if n >= 0 {
        format!("+{}", thousands(n))
    } else {
        thousands(n)
    }
}

pub fn report(app: &App) -> Result<String> {
    let channels = app.config.names();
    let mut handles = BTreeMap::new();
    let mut data: Vec<(String, Daily, bool)> = Vec::new();
    let mut unavailable: Vec<(String, String)> = Vec::new();
    for channel in &channels {
        handles.insert(channel.clone(), app.channel_config(channel)?.handle);
        match daily(app, channel) {
            Ok((series, _)) if series.is_empty() => {
                unavailable.push((channel.clone(), "no analytics yet".into()))
            }
            Ok((series, unmonetized)) => data.push((channel.clone(), series, unmonetized)),
            Err(error) => unavailable.push((channel.clone(), error.to_string())),
        }
    }
    let Some(day) = data.iter().filter_map(|(_, d, _)| d.keys().max()).min() else {
        let mut lines = vec!["Analytics unavailable.".to_owned()];
        lines.extend(
            unavailable
                .iter()
                .map(|(name, reason)| format!("• {}: {reason}", handles[name])),
        );
        return Ok(lines.join("\n"));
    };
    let day: Date = day.parse()?;
    let mut lines = vec![format!("{}  ", day.strftime("%a %-d %b")), String::new()];
    if !unavailable.is_empty() {
        lines.extend([
            "Totals include available channels only.  ".to_owned(),
            String::new(),
        ]);
    }
    type Format = fn(f64) -> String;
    let sections: [(&str, &str, Format, Format); 3] = [
        ("earnings", "Earnings (AUD)", money, money),
        ("views", "Views", |n| thousands(n.round() as i64), compact),
        ("subscribers", "Subscribers", signed, signed),
    ];
    for (metric, heading, one_day, window) in sections {
        let metric_days: Vec<&String> = data
            .iter()
            .filter_map(|(_, d, _)| {
                d.iter()
                    .filter(|(_, values)| values[metric].is_some())
                    .map(|(day, _)| day)
                    .max()
            })
            .collect();
        let Some(metric_day) = metric_days.into_iter().min() else {
            lines.push(format!("{heading}: unavailable  "));
            continue;
        };
        let metric_day: Date = metric_day.parse()?;
        let first = metric_day.checked_sub(jiff::Span::new().days(WINDOW - 1))?;
        let rows: BTreeMap<&String, (Option<f64>, f64)> = data
            .iter()
            .map(|(name, d, _)| {
                (
                    name,
                    (
                        d.get(&metric_day.to_string()).and_then(|v| v[metric]),
                        total(d, metric, first, metric_day),
                    ),
                )
            })
            .collect();
        let on_day: f64 = rows.values().filter_map(|r| r.0).sum();
        let sum: f64 = rows.values().map(|r| r.1).sum();
        lines.push(format!(
            "{heading}: **{}** (Last {WINDOW} days: **{}**)  ",
            one_day(on_day),
            window(sum)
        ));
        for channel in &channels {
            let handle = &handles[channel];
            if let Some((_, reason)) = unavailable.iter().find(|(n, _)| n == channel) {
                lines.push(format!("• {handle}: unavailable ({reason})  "));
                continue;
            }
            match rows.get(channel).and_then(|r| r.0) {
                Some(value) => lines.push(format!("• {handle}: **{}**  ", one_day(value))),
                None => {
                    let unmonetized = data.iter().any(|(n, _, u)| n == channel && *u);
                    let label = if metric == "earnings" && unmonetized {
                        "not monetized"
                    } else {
                        "pending / unavailable"
                    };
                    lines.push(format!("• {handle}: {label}  "));
                }
            }
        }
        lines.push(String::new());
    }
    Ok(lines.join("\n").trim_end().to_owned())
}
