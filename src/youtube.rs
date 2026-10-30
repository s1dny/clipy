//! A channel's YouTube account: its OAuth token, authenticated requests and
//! the cached inventory of its uploads.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow, bail};
use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::app::{App, now, parse_time, write_atomic};

pub const API: &str = "https://youtube.googleapis.com/youtube/v3";
const AUTH_URI: &str = "https://accounts.google.com/o/oauth2/auth";
const TOKEN_URI: &str = "https://oauth2.googleapis.com/token";
const CACHE: Duration = Duration::from_secs(600);

pub const SCOPES: [&str; 4] = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
];

/// No usable token, and nobody to consent interactively.
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct AuthError(pub String);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Token {
    pub client_id: String,
    pub client_secret: String,
    pub refresh_token: String,
    pub scopes: Vec<String>,
    #[serde(default)]
    pub access_token: Option<String>,
    #[serde(default)]
    pub expires_at: Option<jiff::Timestamp>,
}

type Uploads = (Instant, Arc<Vec<Video>>);

#[derive(Default)]
pub struct Cache {
    locks: Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
    videos: Mutex<HashMap<String, Uploads>>,
}

/// One upload on the channel, private and scheduled ones included.
#[derive(Debug, Clone, Serialize)]
pub struct Video {
    pub id: String,
    pub title: String,
    pub description: String,
    pub tags: Vec<String>,
    pub privacy: String,
    pub upload_status: Option<String>,
    pub processing_status: Option<String>,
    pub processing_failure_reason: Option<String>,
    pub failure_reason: Option<String>,
    pub rejection_reason: Option<String>,
    pub uploaded_at: String,
    pub thumbnails: Value,
    pub publish_time: String,
    pub duration_seconds: i64,
    pub views: i64,
    pub likes: i64,
    pub comments: i64,
}

/// A fresh access token for the channel, refreshing and saving it if needed.
pub async fn access_token(app: &App, channel: &str) -> Result<String> {
    let lock = app
        .youtube
        .locks
        .lock()
        .unwrap()
        .entry(channel.to_owned())
        .or_default()
        .clone();
    let _guard = lock.lock().await;
    let path = app.token_file(channel);
    let text = std::fs::read_to_string(&path).map_err(|_| {
        AuthError(format!(
            "No YouTube token at {}; run clipy auth {channel}.",
            path.display()
        ))
    })?;
    let mut token: Token = serde_json::from_str(&text).map_err(|e| {
        AuthError(format!(
            "Unreadable YouTube token ({e}); run clipy auth {channel}."
        ))
    })?;
    let missing: Vec<_> = SCOPES
        .iter()
        .filter(|scope| !token.scopes.iter().any(|s| s == *scope))
        .collect();
    if !missing.is_empty() {
        return Err(AuthError(format!(
            "YouTube token lacks scopes {missing:?}; run clipy auth {channel} again."
        ))
        .into());
    }
    if let (Some(access), Some(expires)) = (&token.access_token, token.expires_at)
        && expires > now() + jiff::SignedDuration::from_secs(60)
    {
        return Ok(access.clone());
    }
    let response = app
        .http
        .post(TOKEN_URI)
        .form(&[
            ("client_id", token.client_id.as_str()),
            ("client_secret", token.client_secret.as_str()),
            ("refresh_token", token.refresh_token.as_str()),
            ("grant_type", "refresh_token"),
        ])
        .send()
        .await
        .map_err(|e| {
            AuthError(format!(
                "YouTube token refresh failed ({e}); try again later."
            ))
        })?;
    let status = response.status();
    let body: Value = response.json().await.unwrap_or_default();
    if !status.is_success() {
        return Err(AuthError(format!(
            "YouTube token refresh failed ({status}: {body}); run clipy auth {channel} again."
        ))
        .into());
    }
    let access = body["access_token"]
        .as_str()
        .ok_or_else(|| AuthError("Token refresh returned no access token.".into()))?
        .to_owned();
    token.expires_at =
        Some(now() + jiff::SignedDuration::from_secs(body["expires_in"].as_i64().unwrap_or(3600)));
    token.access_token = Some(access.clone());
    save_token(&path, &token)?;
    Ok(access)
}

pub fn save_token(path: &std::path::Path, token: &Token) -> Result<()> {
    write_atomic(path, &serde_json::to_vec_pretty(token)?, Some(0o600))
}

/// An authenticated JSON request to a Google API; errors keep the response body.
pub async fn request(
    app: &App,
    channel: &str,
    method: Method,
    url: &str,
    query: &[(&str, String)],
    body: Option<&Value>,
) -> Result<Value> {
    let token = access_token(app, channel).await?;
    let mut builder = app
        .http
        .request(method, url)
        .bearer_auth(token)
        .query(query);
    if let Some(body) = body {
        builder = builder.json(body);
    }
    let response = builder.send().await?;
    read_json(response).await
}

pub async fn read_json(response: reqwest::Response) -> Result<Value> {
    let status = response.status();
    let url = response.url().clone();
    let text = response.text().await?;
    if !status.is_success() {
        bail!(
            "HTTP {status} from {}: {}",
            url.path(),
            crate::app::tail(&text, 2000)
        );
    }
    if text.trim().is_empty() {
        return Ok(Value::Null);
    }
    serde_json::from_str(&text).with_context(|| format!("Invalid JSON from {}", url.path()))
}

pub async fn get(
    app: &App,
    channel: &str,
    resource: &str,
    query: &[(&str, String)],
) -> Result<Value> {
    request(
        app,
        channel,
        Method::GET,
        &format!("{API}/{resource}"),
        query,
        None,
    )
    .await
}

/// The signed-in channel's resource (channels.list mine=true).
pub async fn mine(app: &App, channel: &str, part: &str) -> Result<Value> {
    let response = get(
        app,
        channel,
        "channels",
        &[("part", part.into()), ("mine", "true".into())],
    )
    .await?;
    response["items"]
        .get(0)
        .cloned()
        .ok_or_else(|| anyhow!("The YouTube token for {channel} has no channel."))
}

/// videos.list for up to 50 ids with the given parts.
pub async fn videos(app: &App, channel: &str, ids: &[String], part: &str) -> Result<Vec<Value>> {
    let mut out = Vec::new();
    for batch in ids.chunks(50) {
        let response = get(
            app,
            channel,
            "videos",
            &[("part", part.into()), ("id", batch.join(","))],
        )
        .await?;
        out.extend(response["items"].as_array().cloned().unwrap_or_default());
    }
    Ok(out)
}

/// Every upload on the channel, newest first, cached for ten minutes.
pub async fn channel_videos(app: &App, channel: &str, refresh: bool) -> Result<Arc<Vec<Video>>> {
    if !refresh
        && let Some((at, videos)) = app.youtube.videos.lock().unwrap().get(channel)
        && at.elapsed() < CACHE
    {
        return Ok(videos.clone());
    }
    let account = mine(app, channel, "contentDetails").await?;
    let uploads = account["contentDetails"]["relatedPlaylists"]["uploads"]
        .as_str()
        .context("The channel has no uploads playlist.")?
        .to_owned();
    let mut ids = Vec::new();
    let mut page: Option<String> = None;
    loop {
        let mut query = vec![
            ("part", "contentDetails".to_owned()),
            ("playlistId", uploads.clone()),
            ("maxResults", "50".to_owned()),
        ];
        if let Some(token) = &page {
            query.push(("pageToken", token.clone()));
        }
        let response = get(app, channel, "playlistItems", &query).await?;
        for item in response["items"].as_array().into_iter().flatten() {
            if let Some(id) = item["contentDetails"]["videoId"].as_str() {
                ids.push(id.to_owned());
            }
        }
        match response["nextPageToken"].as_str() {
            Some(token) => page = Some(token.to_owned()),
            None => break,
        }
    }
    let mut list: Vec<Video> = videos(
        app,
        channel,
        &ids,
        "snippet,statistics,contentDetails,status,processingDetails",
    )
    .await?
    .iter()
    .map(video_summary)
    .collect();
    list.sort_by(|a, b| b.publish_time.cmp(&a.publish_time));
    let list = Arc::new(list);
    app.youtube
        .videos
        .lock()
        .unwrap()
        .insert(channel.to_owned(), (Instant::now(), list.clone()));
    Ok(list)
}

fn text(value: &Value) -> Option<String> {
    value.as_str().map(str::to_owned)
}

fn count(value: &Value) -> i64 {
    value
        .as_str()
        .and_then(|s| s.parse().ok())
        .or_else(|| value.as_i64())
        .unwrap_or(0)
}

pub fn video_summary(v: &Value) -> Video {
    let snippet = &v["snippet"];
    let status = &v["status"];
    let processing = &v["processingDetails"];
    let stats = &v["statistics"];
    let published = text(&snippet["publishedAt"]).unwrap_or_default();
    Video {
        id: text(&v["id"]).unwrap_or_default(),
        title: text(&snippet["title"]).unwrap_or_default(),
        description: text(&snippet["description"]).unwrap_or_default(),
        tags: serde_json::from_value(snippet["tags"].clone()).unwrap_or_default(),
        privacy: text(&status["privacyStatus"]).unwrap_or_default(),
        upload_status: text(&status["uploadStatus"]),
        processing_status: text(&processing["processingStatus"]),
        processing_failure_reason: text(&processing["processingFailureReason"]),
        failure_reason: text(&status["failureReason"]),
        rejection_reason: text(&status["rejectionReason"]),
        thumbnails: snippet["thumbnails"].clone(),
        publish_time: text(&status["publishAt"]).unwrap_or_else(|| published.clone()),
        uploaded_at: published,
        duration_seconds: iso_duration_seconds(
            v["contentDetails"]["duration"].as_str().unwrap_or(""),
        ),
        views: count(&stats["viewCount"]),
        likes: count(&stats["likeCount"]),
        comments: count(&stats["commentCount"]),
    }
}

/// "PT1H2M3S" -> 3723; unparseable durations are 0.
pub fn iso_duration_seconds(value: &str) -> i64 {
    static PATTERN: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$").unwrap()
    });
    let Some(captures) = PATTERN.captures(value) else {
        return 0;
    };
    let part = |i: usize| -> i64 {
        captures
            .get(i)
            .and_then(|m| m.as_str().parse().ok())
            .unwrap_or(0)
    };
    ((part(1) * 24 + part(2)) * 60 + part(3)) * 60 + part(4)
}

/// When the item's publish time falls, in Sydney.
pub fn sydney_day(publish_time: &str) -> Result<jiff::civil::Date> {
    Ok(parse_time(publish_time)?
        .to_zoned(crate::app::sydney())
        .date())
}

/// Sign a channel in: print a consent link, wait for Google's redirect to a
/// loopback port, and keep the token only if it belongs to `handle`.
pub async fn authorize(app: &App, channel: &str, handle: &str) -> Result<()> {
    use base64::Engine;
    use sha2::Digest;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let secrets_path =
        std::env::var("YOUTUBE_CLIENT_SECRETS").unwrap_or_else(|_| "client_secrets.json".into());
    let secrets: Value =
        serde_json::from_str(&std::fs::read_to_string(&secrets_path).with_context(|| {
            format!("No OAuth client at {secrets_path}; download one from Google Cloud Console.")
        })?)?;
    let installed = &secrets["installed"];
    let client_id = installed["client_id"]
        .as_str()
        .context("client_id missing")?;
    let client_secret = installed["client_secret"]
        .as_str()
        .context("client_secret missing")?;

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let redirect = format!("http://127.0.0.1:{}/", listener.local_addr()?.port());
    let verifier = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let challenge = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .encode(sha2::Sha256::digest(verifier.as_bytes()));
    let state = uuid::Uuid::new_v4().simple().to_string();
    let mut consent = url::Url::parse(AUTH_URI)?;
    consent
        .query_pairs_mut()
        .append_pair("client_id", client_id)
        .append_pair("redirect_uri", &redirect)
        .append_pair("response_type", "code")
        .append_pair("scope", &SCOPES.join(" "))
        .append_pair("access_type", "offline")
        .append_pair("prompt", "consent")
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256")
        .append_pair("state", &state);
    println!("Sign in as {handle}: {consent}");

    let code = loop {
        let (mut socket, _) = listener.accept().await?;
        let mut buffer = vec![0; 8192];
        let n = socket.read(&mut buffer).await?;
        let request = String::from_utf8_lossy(&buffer[..n]);
        let target = request.split_whitespace().nth(1).unwrap_or("/");
        let url = url::Url::parse(&format!("http://localhost{target}"))?;
        let params: HashMap<_, _> = url.query_pairs().into_owned().collect();
        let reply = "Clipy received Google's response. You can close this tab.";
        socket
            .write_all(
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}",
                    reply.len()
                )
                .as_bytes(),
            )
            .await?;
        if params.get("state") != Some(&state) {
            continue;
        }
        if let Some(error) = params.get("error") {
            bail!("Google refused consent: {error}");
        }
        if let Some(code) = params.get("code") {
            break code.clone();
        }
    };
    let response = app
        .http
        .post(TOKEN_URI)
        .form(&[
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("code", code.as_str()),
            ("code_verifier", verifier.as_str()),
            ("redirect_uri", redirect.as_str()),
            ("grant_type", "authorization_code"),
        ])
        .send()
        .await?;
    let body = read_json(response).await?;
    let token = Token {
        client_id: client_id.into(),
        client_secret: client_secret.into(),
        refresh_token: body["refresh_token"]
            .as_str()
            .context("Google returned no refresh token")?
            .into(),
        scopes: body["scope"]
            .as_str()
            .unwrap_or_default()
            .split_whitespace()
            .map(str::to_owned)
            .collect(),
        access_token: body["access_token"].as_str().map(str::to_owned),
        expires_at: Some(
            now() + jiff::SignedDuration::from_secs(body["expires_in"].as_i64().unwrap_or(3600)),
        ),
    };
    let response = app
        .http
        .get(format!("{API}/channels"))
        .query(&[("part", "snippet"), ("mine", "true")])
        .bearer_auth(token.access_token.clone().unwrap_or_default())
        .send()
        .await?;
    let account = read_json(response).await?;
    let signed_in = account["items"][0]["snippet"]["customUrl"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    if !signed_in.eq_ignore_ascii_case(handle) {
        bail!(
            "Signed in as {}, not {handle}; pick that channel's brand account.",
            if signed_in.is_empty() {
                "no channel"
            } else {
                &signed_in
            }
        );
    }
    let path = app.token_file(channel);
    save_token(&path, &token)?;
    println!("Saved {handle}'s YouTube token to {}", path.display());
    Ok(())
}

/// Set a clip's thumbnail on its uploaded video.
pub async fn set_thumbnail(
    app: &App,
    channel: &str,
    video_id: &str,
    jpeg: &std::path::Path,
) -> Result<()> {
    let token = access_token(app, channel).await?;
    let bytes = tokio::fs::read(jpeg).await?;
    let response = app
        .http
        .post("https://youtube.googleapis.com/upload/youtube/v3/thumbnails/set")
        .query(&[("videoId", video_id), ("uploadType", "media")])
        .bearer_auth(token)
        .header("Content-Type", "image/jpeg")
        .body(bytes)
        .send()
        .await?;
    read_json(response).await?;
    Ok(())
}
