//! A discovery-driven Google API interface using a channel's OAuth token.
//!
//! No endpoint wrappers or handwritten method tables: Google's published
//! discovery documents define resource paths, parameters and HTTP verbs.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, bail};
use base64::Engine;
use reqwest::Method;
use rusqlite::params;
use serde_json::{Value, json};

use crate::app::{App, now};
use crate::youtube;

const DISCOVERY: &str = "https://www.googleapis.com/discovery/v1/apis";

static DOCUMENTS: std::sync::LazyLock<Mutex<HashMap<String, Arc<Value>>>> =
    std::sync::LazyLock::new(Default::default);

/// The official discovery document for an API version, fetched once.
pub async fn document(app: &App, api: &str, version: &str) -> Result<Arc<Value>> {
    if !api
        .chars()
        .chain(version.chars())
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.')
    {
        bail!("Unknown Google API {api}/{version}.");
    }
    let key = format!("{api}/{version}");
    if let Some(document) = DOCUMENTS.lock().unwrap().get(&key) {
        return Ok(document.clone());
    }
    let response = app
        .http
        .get(format!("{DISCOVERY}/{api}/{version}/rest"))
        .send()
        .await?;
    if !response.status().is_success() {
        bail!(
            "No official discovery document for {api}/{version} ({}).",
            response.status()
        );
    }
    let document = Arc::new(response.json::<Value>().await?);
    DOCUMENTS.lock().unwrap().insert(key, document.clone());
    Ok(document)
}

fn escape(token: &str) -> String {
    token.replace('~', "~0").replace('/', "~1")
}

/// Search the spec by literal text with lossless pagination, or return the
/// exact subtree at a JSON Pointer when `query` is None.
pub fn specification(
    document: &Value,
    pointer: &str,
    query: Option<&str>,
    offset: usize,
    limit: usize,
) -> Result<Value> {
    let value = if pointer.is_empty() {
        document
    } else {
        if !pointer.starts_with('/') {
            bail!("pointer must be a JSON Pointer starting with /.");
        }
        document
            .pointer(pointer)
            .with_context(|| format!("Nothing at {pointer}."))?
    };
    let Some(query) = query else {
        return Ok(json!({ "pointer": pointer, "value": value }));
    };
    if !(1..=200).contains(&limit) {
        bail!("offset must be nonnegative and limit between 1 and 200.");
    }
    let needle = query.to_lowercase();
    let mut matches = Vec::new();
    fn walk(node: &Value, path: &str, needle: &str, matches: &mut Vec<Value>) {
        match node {
            Value::Object(map) => {
                for (key, child) in map {
                    walk(child, &format!("{path}/{}", escape(key)), needle, matches);
                }
            }
            Value::Array(items) => {
                for (index, child) in items.iter().enumerate() {
                    walk(child, &format!("{path}/{index}"), needle, matches);
                }
            }
            leaf => {
                let text = match leaf {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                };
                if path.to_lowercase().contains(needle) || text.to_lowercase().contains(needle) {
                    matches.push(json!({ "pointer": path, "value": leaf }));
                }
            }
        }
    }
    walk(value, pointer, &needle, &mut matches);
    let stop = (offset + limit).min(matches.len());
    Ok(json!({
        "matches": matches.get(offset.min(stop)..stop).unwrap_or_default(),
        "match_count": matches.len(),
        "next_offset": if stop < matches.len() { Some(stop) } else { None },
    }))
}

/// Expand "{name}" (escaped) and "{+name}" (reserved) path templates.
fn expand(template: &str, parameters: &mut serde_json::Map<String, Value>) -> Result<String> {
    use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
    const SIMPLE: &AsciiSet = &NON_ALPHANUMERIC
        .remove(b'-')
        .remove(b'_')
        .remove(b'.')
        .remove(b'~');
    const RESERVED: &AsciiSet = &SIMPLE
        .remove(b'/')
        .remove(b':')
        .remove(b'@')
        .remove(b'!')
        .remove(b'$')
        .remove(b'&')
        .remove(b'\'')
        .remove(b'(')
        .remove(b')')
        .remove(b'*')
        .remove(b'+')
        .remove(b',')
        .remove(b';')
        .remove(b'=');
    let mut out = String::new();
    let mut rest = template;
    while let Some(start) = rest.find('{') {
        out.push_str(&rest[..start]);
        let end = rest[start..].find('}').context("malformed path template")? + start;
        let (reserved, name) = match rest[start + 1..end].strip_prefix('+') {
            Some(name) => (true, name),
            None => (false, &rest[start + 1..end]),
        };
        let value = parameters
            .remove(name)
            .with_context(|| format!("Missing required parameter {name}."))?;
        let text = match value {
            Value::String(s) => s,
            other => other.to_string(),
        };
        out.push_str(
            &utf8_percent_encode(&text, if reserved { RESERVED } else { SIMPLE }).to_string(),
        );
        rest = &rest[end + 1..];
    }
    out.push_str(rest);
    Ok(out)
}

fn query_values(value: &Value) -> Vec<String> {
    match value {
        Value::Array(items) => items.iter().flat_map(query_values).collect(),
        Value::String(s) => vec![s.clone()],
        Value::Null => vec![],
        other => vec![other.to_string()],
    }
}

/// Execute a discovered method. Reads return the raw service response;
/// mutations are journaled before they are sent and never retried.
#[allow(clippy::too_many_arguments)]
pub async fn call(
    app: &App,
    channel: &str,
    api: &str,
    version: &str,
    method: &str,
    parameters: serde_json::Map<String, Value>,
    mutation: bool,
    media_path: Option<&str>,
    observation: Option<Value>,
) -> Result<Value> {
    let document = document(app, api, version).await?;
    let mut node = &*document;
    let parts: Vec<&str> = method.split('.').collect();
    for name in &parts[..parts.len() - 1] {
        node = node["resources"]
            .get(*name)
            .with_context(|| format!("{api}/{version} has no resource {name}."))?;
    }
    let spec = node["methods"]
        .get(*parts.last().unwrap())
        .with_context(|| format!("{api}/{version} has no method {method}."))?;
    let verb = spec["httpMethod"].as_str().unwrap_or("GET");
    let read_only = ["GET", "HEAD", "OPTIONS"].contains(&verb);
    if read_only == mutation {
        bail!("Use google_api_read for read verbs and google_api_write for mutations.");
    }
    let known = |name: &str| {
        spec["parameters"]
            .get(name)
            .or_else(|| document["parameters"].get(name))
    };
    for name in parameters.keys() {
        if name != "body" && known(name).is_none() {
            bail!("{method} has no parameter {name}.");
        }
    }
    for (name, definition) in spec["parameters"].as_object().into_iter().flatten() {
        if definition["required"] == true && !parameters.contains_key(name) {
            bail!("{method} requires parameter {name}.");
        }
    }
    let mut remaining = parameters.clone();
    let body = remaining.remove("body");
    let download = read_only && remaining.get("alt").and_then(Value::as_str) == Some("media");
    if download && spec["supportsMediaDownload"] != true {
        bail!("This method does not support media downloads.");
    }
    let root = document["rootUrl"]
        .as_str()
        .unwrap_or_default()
        .trim_end_matches('/')
        .to_owned();
    let media = match media_path {
        None => None,
        Some(path) => {
            if !mutation {
                bail!("Media must be a file inside this channel's directory.");
            }
            let resolved = std::fs::canonicalize(path)
                .context("Media must be a file inside this channel's directory.")?;
            let inside = app
                .media_roots(channel)
                .iter()
                .filter_map(|root| std::fs::canonicalize(root).ok())
                .any(|root| resolved.starts_with(root));
            if !inside || !resolved.is_file() {
                bail!("Media must be a file inside this channel's directory.");
            }
            let upload = spec["mediaUpload"]["protocols"]["simple"]["path"]
                .as_str()
                .context("This method does not accept media uploads.")?;
            Some((resolved, upload.to_owned()))
        }
    };
    let path = match &media {
        Some((_, upload)) => expand(upload, &mut remaining)?,
        None => format!(
            "/{}{}",
            document["servicePath"].as_str().unwrap_or_default(),
            expand(spec["path"].as_str().unwrap_or_default(), &mut remaining)?
        ),
    };
    let mut query: Vec<(String, String)> = remaining
        .iter()
        .flat_map(|(name, value)| {
            query_values(value)
                .into_iter()
                .map(move |v| (name.clone(), v))
        })
        .collect();
    let token = youtube::access_token(app, channel).await?;
    let mut request = app
        .http
        .request(
            Method::from_bytes(verb.as_bytes())?,
            format!("{root}{path}"),
        )
        .bearer_auth(token);
    match (&media, &body) {
        (Some((file, _)), None) => {
            query.push(("uploadType".into(), "media".into()));
            request = request
                .header("Content-Type", mime(file))
                .body(tokio::fs::read(file).await?);
        }
        (Some((file, _)), Some(body)) => {
            query.push(("uploadType".into(), "multipart".into()));
            let boundary = format!("clipy{}", uuid::Uuid::new_v4().simple());
            let mut bytes = format!(
                "--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{body}\r\n--{boundary}\r\nContent-Type: {}\r\n\r\n",
                mime(file)
            )
            .into_bytes();
            bytes.extend(tokio::fs::read(file).await?);
            bytes.extend(format!("\r\n--{boundary}--").into_bytes());
            request = request
                .header(
                    "Content-Type",
                    format!("multipart/related; boundary={boundary}"),
                )
                .body(bytes);
        }
        (None, Some(body)) => request = request.json(body),
        (None, None) => {}
    }
    let request = request.query(&query);
    if !mutation {
        let response = request.send().await?;
        if download {
            let status = response.status();
            let bytes = response.bytes().await?;
            if !status.is_success() {
                bail!("HTTP {status}: {}", String::from_utf8_lossy(&bytes));
            }
            return Ok(match String::from_utf8(bytes.to_vec()) {
                Ok(text) => text.into(),
                Err(_) => json!({
                    "encoding": "base64",
                    "content": base64::engine::general_purpose::STANDARD.encode(&bytes),
                }),
            });
        }
        return youtube::read_json(response).await;
    }
    // Persist intent BEFORE the external write. A lost response is ambiguous,
    // so the agent must inspect the remote state before retrying it.
    let id = uuid::Uuid::new_v4().simple().to_string();
    let journal = (
        id.clone(),
        channel.to_owned(),
        api.to_owned(),
        version.to_owned(),
        method.to_owned(),
        Value::Object(parameters).to_string(),
        media
            .as_ref()
            .map(|(file, _)| file.to_string_lossy().into_owned()),
        observation.map(|o| o.to_string()),
    );
    app.db
        .call(move |c| {
            c.execute(
                "INSERT INTO api_writes (id, channel, api, version, method, parameters, media_path, observation,
                    status, started_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'started', ?9)",
                params![journal.0, journal.1, journal.2, journal.3, journal.4, journal.5, journal.6, journal.7, now().to_string()],
            )?;
            Ok(())
        })
        .await?;
    let outcome = match request.send().await {
        Ok(response) => youtube::read_json(response).await,
        Err(error) => Err(error.into()),
    };
    let finish = (
        id.clone(),
        outcome
            .as_ref()
            .map(Value::to_string)
            .map_err(|e| format!("{e:#}")),
    );
    app.db
        .call(move |c| {
            match finish.1 {
                Ok(response) => c.execute(
                    "UPDATE api_writes SET status = 'succeeded', response = ?2, finished_at = ?3 WHERE id = ?1",
                    params![finish.0, response, now().to_string()],
                )?,
                Err(error) => c.execute(
                    "UPDATE api_writes SET status = 'unconfirmed', error = ?2, finished_at = ?3 WHERE id = ?1",
                    params![finish.0, error, now().to_string()],
                )?,
            };
            Ok(())
        })
        .await?;
    Ok(json!({ "response": outcome?, "audit_id": id }))
}

fn mime(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|e| e.to_str())
        .map(str::to_lowercase)
        .as_deref()
    {
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("png") => "image/png",
        Some("mp4") => "video/mp4",
        Some("srt") => "application/x-subrip",
        _ => "application/octet-stream",
    }
}
