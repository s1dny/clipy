//! The shared context every command runs with: where data lives, the
//! database, configuration and the HTTP client.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use jiff::tz::TimeZone;

use crate::config::{ChannelConfig, Config};
use crate::db::Db;

pub struct App {
    /// Channel media, credentials, thumbnail templates, headshots and the
    /// database.
    pub data: PathBuf,
    /// config.toml, read once at start; a changed file applies on restart.
    pub config: Config,
    pub db: Db,
    pub http: reqwest::Client,
    /// Timezone for notebook entries.
    pub timezone: TimeZone,
    pub youtube: crate::youtube::Cache,
}

impl App {
    pub fn from_env() -> Result<Arc<Self>> {
        let data = PathBuf::from(std::env::var("CLIPY_DATA_DIR").unwrap_or_else(|_| ".".into()));
        let config_path = match std::env::var("CLIPY_CONFIG") {
            Ok(path) => PathBuf::from(path),
            Err(_) => std::env::current_dir()?.join("config.toml"),
        };
        let config = Config::load(&config_path)?;
        let timezone_name =
            std::env::var("CLIPY_TIMEZONE").unwrap_or_else(|_| "Australia/Sydney".into());
        std::fs::create_dir_all(&data)?;
        Ok(Arc::new(Self {
            db: Db::open(&data.join("clipy.db"))?,
            data,
            config,
            http: http_client(Duration::from_secs(120))?,
            timezone: TimeZone::get(&timezone_name)
                .with_context(|| format!("Unknown CLIPY_TIMEZONE {timezone_name}"))?,
            youtube: Default::default(),
        }))
    }

    /// A channel's settings with the agent's prompt overrides applied.
    pub fn channel_config(&self, channel: &str) -> Result<ChannelConfig> {
        let overrides: BTreeMap<String, String> = self.db.with(|c| {
            let mut statement =
                c.prepare("SELECT key, value FROM prompt_overrides WHERE channel = ?1")?;
            let rows = statement
                .query_map([channel], |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?;
            Ok(rows)
        })?;
        self.config.channel(channel, &overrides)
    }

    pub fn channel_dir(&self, channel: &str) -> PathBuf {
        self.data.join("channels").join(channel)
    }

    pub fn token_file(&self, channel: &str) -> PathBuf {
        self.channel_dir(channel).join("youtube-token.json")
    }

    /// Where a rendered clip's video, thumbnail and card images live.
    pub fn clip_dir(&self, channel: &str, source_id: &str, number: i64) -> PathBuf {
        self.channel_dir(channel)
            .join("clips")
            .join(format!("{source_id}-{number:02}"))
    }

    /// Downloaded source media, shared by every channel.
    pub fn source_dir(&self, video_id: &str) -> PathBuf {
        self.data.join("sources").join(video_id)
    }

    pub fn headshots_dir(&self) -> PathBuf {
        self.data.join("headshots")
    }

    /// Files the agent may upload through google_api_write.
    pub fn media_roots(&self, channel: &str) -> [PathBuf; 2] {
        let dir = self.channel_dir(channel);
        [dir.join("clips"), dir.join("artwork")]
    }
}

/// Every outbound HTTP client starts here. Certificates in $SSL_CERT_FILE
/// are trusted alongside the platform's roots.
pub fn http_builder() -> Result<reqwest::ClientBuilder> {
    let mut builder = reqwest::Client::builder()
        .user_agent("clipy")
        .connect_timeout(Duration::from_secs(30));
    if let Ok(path) = std::env::var("SSL_CERT_FILE") {
        let pem = std::fs::read(&path).with_context(|| format!("read {path}"))?;
        builder = builder.tls_certs_merge(reqwest::Certificate::from_pem_bundle(&pem)?);
    }
    Ok(builder)
}

pub fn http_client(timeout: Duration) -> Result<reqwest::Client> {
    Ok(http_builder()?.read_timeout(timeout).build()?)
}

/// The 11-character id of a YouTube video URL.
pub fn video_id(url: &str) -> Result<String> {
    let parsed = url::Url::parse(url).with_context(|| format!("Not a URL: {url}"))?;
    let id = match parsed.host_str() {
        Some("www.youtube.com" | "youtube.com" | "m.youtube.com") => parsed
            .query_pairs()
            .find(|(key, _)| key == "v")
            .map(|(_, value)| value.into_owned()),
        Some("youtu.be") => Some(parsed.path().trim_start_matches('/').to_owned()),
        _ => None,
    };
    match id {
        Some(id) if is_video_id(&id) => Ok(id),
        _ => bail!("Cannot extract a video ID from URL: {url}"),
    }
}

pub fn is_video_id(id: &str) -> bool {
    id.len() == 11
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

pub fn watch_url(video_id: &str) -> String {
    format!("https://www.youtube.com/watch?v={video_id}")
}

/// Write a file atomically: a temporary sibling renamed into place.
pub fn write_atomic(path: &Path, bytes: &[u8], mode: Option<u32>) -> Result<()> {
    use std::io::Write;
    let parent = path.parent().context("path has no parent")?;
    std::fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name().unwrap_or_default().to_string_lossy(),
        uuid::Uuid::new_v4().simple()
    ));
    let mut file = std::fs::File::create(&temporary)?;
    #[cfg(unix)]
    if let Some(mode) = mode {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(std::fs::Permissions::from_mode(mode))?;
    }
    let result = file.write_all(bytes).and_then(|()| file.sync_all());
    if let Err(error) = result {
        let _ = std::fs::remove_file(&temporary);
        return Err(error.into());
    }
    std::fs::rename(&temporary, path)?;
    Ok(())
}

pub fn now() -> jiff::Timestamp {
    jiff::Timestamp::now()
}

/// Parse an RFC 3339 timestamp; a time without an offset is UTC.
pub fn parse_time(value: &str) -> Result<jiff::Timestamp> {
    if let Ok(timestamp) = value.parse::<jiff::Timestamp>() {
        return Ok(timestamp);
    }
    let civil: jiff::civil::DateTime = value
        .parse()
        .with_context(|| format!("Not an ISO 8601 time: {value}"))?;
    Ok(civil.to_zoned(TimeZone::UTC)?.timestamp())
}

pub fn sydney() -> TimeZone {
    TimeZone::get("Australia/Sydney").expect("bundled tz database has Australia/Sydney")
}

/// "1,234,567"
pub fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

/// Raise an error message for a failed command, keeping its stderr tail.
pub fn tail(text: &str, max: usize) -> &str {
    let start = text.len().saturating_sub(max);
    let mut start = start;
    while !text.is_char_boundary(start) {
        start += 1;
    }
    &text[start..]
}
