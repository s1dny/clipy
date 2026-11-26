//! YouTube Studio: hourly analytics read directly over HTTP with the signed-in
//! session's cookies, and channel-scoped Studio requests executed by Studio's
//! own client inside the private browser.
//!
//! Chromium is only used to recover a login and to run Studio's native
//! request service; analytics collection never renders pages.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use rusqlite::params;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::app::{App, now, write_atomic};
use crate::cdp;
use crate::youtube;

pub const ORIGIN: &str = "https://studio.youtube.com";
const SOURCE: &str = "YouTube Studio internal analytics API";
const REQUEST_SECONDS: i64 = 150;

/// (Studio metric, our name, divisor), in request order.
const METRICS: [(&str, &str, f64); 6] = [
    ("EXTERNAL_VIEWS", "views", 1.0),
    ("EXTERNAL_WATCH_TIME", "watch_hours", 3_600_000.0),
    ("SUBSCRIBERS_NET_CHANGE", "subscribers", 1.0),
    ("TOTAL_ESTIMATED_EARNINGS", "revenue_usd", 1_000.0),
    ("VIDEO_THUMBNAIL_IMPRESSIONS", "impressions", 1.0),
    ("VIDEO_THUMBNAIL_IMPRESSIONS_VTR", "ctr_percent", 1.0),
];

/// Google needs a valid session, or this account lacks channel access.
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct AuthRequired(String);

fn auth(message: &str) -> anyhow::Error {
    AuthRequired(message.into()).into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Cookie {
    pub name: String,
    pub value: String,
    pub domain: String,
    pub path: String,
    #[serde(default)]
    pub secure: bool,
    #[serde(default)]
    pub http_only: bool,
    /// Seconds since the epoch; -1 for a session cookie.
    #[serde(default = "session_expiry")]
    pub expires: f64,
    /// Attributes the browser reported that we carry along unchanged.
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

fn session_expiry() -> f64 {
    -1.0
}

fn allowed(domain: &str) -> bool {
    let domain = domain.trim_start_matches('.');
    ["google.com", "google.com.au", "google.nl", "youtube.com"]
        .iter()
        .any(|root| domain == *root || domain.ends_with(&format!(".{root}")))
}

#[derive(Default)]
struct Jar(Vec<Cookie>);

impl Jar {
    fn set(&mut self, cookie: Cookie) {
        self.0.retain(|c| {
            !(c.domain == cookie.domain && c.path == cookie.path && c.name == cookie.name)
        });
        let expired = cookie.expires != -1.0 && cookie.expires <= now().as_second() as f64;
        if allowed(&cookie.domain) && !expired {
            self.0.push(cookie);
        }
    }

    fn matching(&self, url: &url::Url) -> impl Iterator<Item = &Cookie> {
        let host = url.host_str().unwrap_or_default().to_owned();
        let path = url.path().to_owned();
        let secure = url.scheme() == "https";
        let now = now().as_second() as f64;
        self.0.iter().filter(move |c| {
            let domain_ok = match c.domain.strip_prefix('.') {
                Some(d) => host == d || host.ends_with(&format!(".{d}")),
                None => host == c.domain,
            };
            domain_ok
                && path.starts_with(&c.path)
                && (secure || !c.secure)
                && (c.expires == -1.0 || c.expires > now)
        })
    }

    fn header(&self, url: &url::Url) -> String {
        self.matching(url)
            .map(|c| format!("{}={}", c.name, c.value))
            .collect::<Vec<_>>()
            .join("; ")
    }

    fn get(&self, name: &str, url: &url::Url) -> Option<String> {
        self.matching(url)
            .find(|c| c.name == name)
            .map(|c| c.value.clone())
    }

    /// Apply a response's Set-Cookie headers.
    fn update(&mut self, url: &url::Url, headers: &reqwest::header::HeaderMap) {
        for header in headers.get_all(reqwest::header::SET_COOKIE) {
            let Ok(text) = header.to_str() else { continue };
            let Ok(parsed) = cookie::Cookie::parse(text.to_owned()) else {
                continue;
            };
            let domain = match parsed.domain() {
                Some(d) => format!(".{}", d.trim_start_matches('.')),
                None => url.host_str().unwrap_or_default().to_owned(),
            };
            let expires = if let Some(age) = parsed.max_age() {
                now().as_second() as f64 + age.whole_seconds() as f64
            } else if let Some(at) = parsed.expires_datetime() {
                at.unix_timestamp() as f64
            } else {
                -1.0
            };
            let previous = self
                .0
                .iter()
                .find(|c| c.domain == domain && c.name == parsed.name())
                .map(|c| c.extra.clone())
                .unwrap_or_default();
            self.set(Cookie {
                name: parsed.name().into(),
                value: parsed.value().into(),
                domain,
                path: parsed.path().unwrap_or("/").into(),
                secure: parsed.secure().unwrap_or(false),
                http_only: parsed.http_only().unwrap_or(false),
                expires,
                extra: previous,
            });
        }
    }
}

fn session_path() -> Option<PathBuf> {
    std::env::var("CLIPY_STUDIO_SESSION")
        .ok()
        .map(PathBuf::from)
}

/// An HTTP client signed in to Studio with the browser's session cookies.
pub struct Client {
    http: reqwest::Client,
    jar: Jar,
    configs: BTreeMap<String, Value>,
    user_agent: String,
}

impl Client {
    pub async fn new() -> Result<Self> {
        let http = crate::app::http_builder()?
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(45))
            .build()?;
        let mut jar = Jar::default();
        let session = session_path();
        if let Some(path) = &session
            && let Ok(text) = std::fs::read_to_string(path)
        {
            for cookie in serde_json::from_str::<Vec<Cookie>>(&text)? {
                jar.set(cookie);
            }
        }
        // Browser metadata holds no credentials. Keep its real user agent for
        // when the browser is temporarily down.
        let metadata = session
            .as_ref()
            .map(|p| p.with_file_name("studio-client.json"));
        let mut user_agent = metadata
            .as_ref()
            .and_then(|m| std::fs::read_to_string(m).ok())
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .and_then(|v| v["user_agent"].as_str().map(str::to_owned))
            .unwrap_or_else(|| "Mozilla/5.0".into());
        let probe = crate::app::http_client(Duration::from_secs(3))?;
        if let Ok(version) = cdp::version(&probe).await
            && let Some(agent) = version["User-Agent"].as_str()
        {
            user_agent = agent.to_owned();
            if let Some(metadata) = &metadata {
                let _ = write_atomic(
                    metadata,
                    json!({"user_agent": agent}).to_string().as_bytes(),
                    Some(0o600),
                );
            }
        }
        Ok(Self {
            http,
            jar,
            configs: BTreeMap::new(),
            user_agent,
        })
    }

    async fn send(
        &mut self,
        request: reqwest::RequestBuilder,
        url: &url::Url,
    ) -> Result<reqwest::Response> {
        let request = request
            .header(reqwest::header::USER_AGENT, &self.user_agent)
            .header(reqwest::header::COOKIE, self.jar.header(url));
        let response = request.send().await?;
        self.jar.update(url, response.headers());
        Ok(response)
    }

    /// Read the channel page's client configuration.
    async fn bootstrap(&mut self, channel_id: &str) -> Result<Value> {
        let mut url = url::Url::parse(&format!("{ORIGIN}/channel/{channel_id}"))?;
        let mut response = None;
        for _ in 0..10 {
            let reply = self.send(self.http.get(url.clone()), &url.clone()).await?;
            if reply.status().is_redirection() {
                let location = reply
                    .headers()
                    .get("location")
                    .and_then(|l| l.to_str().ok())
                    .unwrap_or_default();
                url = url.join(location)?;
                if url.host_str() != Some("studio.youtube.com") {
                    return Err(auth("Google session is signed out"));
                }
                continue;
            }
            response = Some(reply);
            break;
        }
        let response = response.context("Too many Studio redirects")?;
        if [401, 403].contains(&response.status().as_u16()) {
            return Err(auth("Google session is signed out"));
        }
        let html = response.error_for_status()?.text().await?;
        let mut config = Map::new();
        for (index, _) in html.match_indices("ytcfg.set(") {
            let rest = &html[index + "ytcfg.set(".len()..];
            if let Some(Ok(Value::Object(values))) = serde_json::Deserializer::from_str(rest)
                .into_iter::<Value>()
                .next()
            {
                config.extend(values);
            }
        }
        if config.get("INNERTUBE_API_KEY").is_none() {
            return Err(auth("No authenticated Studio client context"));
        }
        if config["DELEGATION_CONTEXT"]["externalChannelId"] != channel_id {
            return Err(auth("Studio account cannot access this channel"));
        }
        let config = Value::Object(config);
        self.configs.insert(channel_id.to_owned(), config.clone());
        Ok(config)
    }

    /// An authenticated Studio request; never retries a possibly applied write.
    pub async fn request(
        &mut self,
        channel_id: &str,
        endpoint: &str,
        body: &Value,
    ) -> Result<Value> {
        check_endpoint(endpoint, body)?;
        let config = match self.configs.get(channel_id) {
            Some(config) => config.clone(),
            None => self.bootstrap(channel_id).await?,
        };
        let url = url::Url::parse(&format!("{ORIGIN}/youtubei/v1/{endpoint}"))?;
        let sapisid = self
            .jar
            .get("SAPISID", &url)
            .ok_or_else(|| auth("YouTube authentication cookie is missing"))?;
        let stamp = now().as_second().to_string();
        let digest = {
            use sha1::Digest;
            hex::encode(sha1::Sha1::digest(
                format!("{stamp} {sapisid} {ORIGIN}").as_bytes(),
            ))
        };
        let mut context = config["INNERTUBE_CONTEXT"].clone();
        context["user"]["delegationContext"] = config["DELEGATION_CONTEXT"].clone();
        let serialized = config["INNERTUBE_CONTEXT_SERIALIZED_DELEGATION_CONTEXT"]
            .as_str()
            .map(str::to_owned);
        if let Some(serialized) = &serialized {
            context["user"]["serializedDelegationContext"] = serialized.clone().into();
        }
        let mut payload = body.clone();
        payload["context"] = context;
        let header = |v: &Value| match v {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        let mut request = self
            .http
            .post(url.clone())
            .query(&[
                ("key", header(&config["INNERTUBE_API_KEY"])),
                ("alt", "json".into()),
            ])
            .header("Authorization", format!("SAPISIDHASH {stamp}_{digest}"))
            .header("Origin", ORIGIN)
            .header("X-Origin", ORIGIN)
            .header(
                "X-Goog-Authuser",
                config
                    .get("SESSION_INDEX")
                    .map(header)
                    .unwrap_or_else(|| "0".into()),
            )
            .header(
                "X-Youtube-Client-Name",
                header(&config["INNERTUBE_CONTEXT_CLIENT_NAME"]),
            )
            .header(
                "X-Youtube-Client-Version",
                header(&config["INNERTUBE_CONTEXT_CLIENT_VERSION"]),
            )
            .json(&payload);
        if let Some(serialized) = &serialized {
            request = request.header("X-Youtube-Delegation-Context", serialized);
        }
        let response = self.send(request, &url).await?;
        if [401, 403].contains(&response.status().as_u16()) {
            return Err(auth("Studio rejected authentication"));
        }
        let payload: Value = response.error_for_status()?.json().await?;
        if !payload["responseContext"]["webResponseContextExtensionData"]["challenge"].is_null() {
            return Err(auth(
                "Studio requires an authentication challenge; the write is unconfirmed. Recheck the remote setting before retrying.",
            ));
        }
        if let Some(error) = payload.get("error") {
            bail!("Studio request failed: {error}");
        }
        Ok(payload)
    }

    /// Key-metric cards for the channel (a daily series between `range`
    /// dates) or for one video (since published).
    async fn cards(
        &mut self,
        channel_id: &str,
        video_id: Option<&str>,
        currency: &str,
        range: Option<(jiff::civil::Date, jiff::civil::Date)>,
        include_revenue: bool,
    ) -> Result<(Value, bool)> {
        let entity = match video_id {
            Some(id) => json!({"videoId": id}),
            None => json!({"channelId": channel_id}),
        };
        let number =
            |d: jiff::civil::Date| d.strftime("%Y%m%d").to_string().parse::<i64>().unwrap_or(0);
        let period = match range {
            Some((start, end)) => {
                json!({"dateIdRange": {"inclusiveStart": number(start), "exclusiveEnd": number(end)}})
            }
            None => json!({
                "referencePoint": "TIME_PERIOD_REFERENCE_POINT_SINCE_PUBLISH",
                "timePeriodType": "ANALYTICS_TIME_PERIOD_TYPE_SINCE_PUBLISH",
                "entity": entity,
            }),
        };
        let offset = now().to_zoned(crate::app::sydney()).offset().seconds();
        let tabs: Vec<Value> = METRICS
            .iter()
            .filter(|(metric, _, _)| include_revenue || *metric != "TOTAL_ESTIMATED_EARNINGS")
            .map(|(metric, _, _)| json!({"metric": metric}))
            .collect();
        let query = json!({
            "screenConfig": {"entity": entity, "timePeriod": period, "currency": currency, "timeZoneOffsetSecs": offset},
            "cardConfigs": [{
                "autoUpdateInterval": "ANALYTICS_AUTO_UPDATE_INTERVAL_NEVER",
                "keyMetricCardConfig": {"timePeriod": {}, "metricTabConfigs": tabs},
                "failureMode": "ANALYTICS_CARD_FAILURE_MODE_FAIL_PAGE",
                "disableSeeMoreButton": true,
                "topEntitySelection": {"channelId": channel_id},
            }],
            "fetchingType": "FETCHING_TYPE_FOREGROUND",
        });
        let payload = self
            .request(channel_id, "yta_web/get_cards", &query)
            .await?;
        let reporting = jiff::tz::TimeZone::get("America/Los_Angeles")?;
        let mut totals = Map::new();
        let mut daily: BTreeMap<String, Map<String, Value>> = BTreeMap::new();
        let mut estimated = false;
        for card in payload["cards"].as_array().into_iter().flatten() {
            if card["config"]["topEntitySelection"]["channelId"] != channel_id {
                bail!("Studio response does not match the requested channel");
            }
            for tab in card["keyMetricCardData"]["keyMetricTabs"]
                .as_array()
                .into_iter()
                .flatten()
            {
                let content = &tab["primaryContent"];
                let Some(&(metric, name, divisor)) =
                    METRICS.iter().find(|(m, _, _)| content["metric"] == *m)
                else {
                    continue;
                };
                let name = if metric == "TOTAL_ESTIMATED_EARNINGS" {
                    format!("revenue_{}", currency.to_lowercase())
                } else {
                    name.to_owned()
                };
                totals.insert(
                    name.clone(),
                    content["total"].as_f64().map(|t| t / divisor).into(),
                );
                let anomalies = content["anomalies"].as_array().cloned().unwrap_or_default();
                estimated |= !anomalies.is_empty();
                if range.is_some() {
                    for datum in content["mainSeries"]["datums"]
                        .as_array()
                        .into_iter()
                        .flatten()
                    {
                        let stamp = datum["x"].as_f64().unwrap_or(0.0);
                        let day = jiff::Timestamp::from_millisecond(stamp as i64)?
                            .to_zoned(reporting.clone())
                            .date();
                        let missing = anomalies.iter().any(|a| {
                            a["impact"] == "ANALYTICS_ANOMALY_IMPACT_DATA_MISSING"
                                && a["startDate"].as_f64().unwrap_or(f64::NEG_INFINITY) <= stamp
                                && stamp < a["endDate"].as_f64().unwrap_or(f64::INFINITY)
                        });
                        let value = datum["y"]
                            .as_f64()
                            .filter(|_| !missing)
                            .map(|y| y / divisor);
                        daily
                            .entry(day.to_string())
                            .or_default()
                            .insert(name.clone(), value.into());
                    }
                }
            }
        }
        if range.is_some() {
            if daily.is_empty() {
                bail!("Studio returned no channel daily series");
            }
            return Ok((json!(daily), estimated));
        }
        if totals.is_empty() {
            bail!("Studio returned no metric totals");
        }
        let metrics: Map<String, Value> = METRICS
            .iter()
            .map(|(_, name, _)| {
                (
                    name.to_string(),
                    totals.get(*name).cloned().unwrap_or(Value::Null),
                )
            })
            .collect();
        Ok((Value::Object(metrics), estimated))
    }

    /// Cards, refreshing the client context and then the browser's cookies
    /// once each on an authentication failure. A revoked login is reported,
    /// never disguised as a zero reading.
    async fn readings(
        &mut self,
        channel_id: &str,
        video_id: Option<&str>,
        currency: &str,
        range: Option<(jiff::civil::Date, jiff::civil::Date)>,
        include_revenue: bool,
    ) -> Result<(Value, bool)> {
        for attempt in 0..3 {
            match self
                .cards(channel_id, video_id, currency, range, include_revenue)
                .await
            {
                Err(error) if error.is::<AuthRequired>() && attempt < 2 => {
                    self.configs.remove(channel_id);
                    if attempt == 1 {
                        self.recover_browser_login().await?;
                    }
                }
                result => return result,
            }
        }
        unreachable!()
    }

    /// Read the owner's current browser cookie jar; no pages are opened.
    async fn recover_browser_login(&mut self) -> Result<()> {
        let http = crate::app::http_client(Duration::from_secs(10))?;
        let browser = cdp::Browser::connect(&http).await?;
        let cookies = browser.cookies().await?;
        self.jar = Jar::default();
        for cookie in cookies {
            if let Ok(cookie) = serde_json::from_value::<Cookie>(cookie) {
                self.jar.set(cookie);
            }
        }
        self.configs.clear();
        Ok(())
    }

    /// Save the cookie jar, only after an authenticated, channel-checked response.
    fn checkpoint(&self) -> Result<()> {
        let Some(path) = session_path() else {
            return Ok(());
        };
        if self.jar.0.iter().any(|c| c.name == "SID") {
            write_atomic(&path, &serde_json::to_vec(&self.jar.0)?, Some(0o600))?;
        }
        Ok(())
    }
}

fn check_endpoint(endpoint: &str, payload: &Value) -> Result<()> {
    static PATTERN: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new("^[a-z_]+/[a-z_]+$").unwrap());
    if !PATTERN.is_match(endpoint) {
        bail!("Use a Studio API resource/method endpoint.");
    }
    if payload.get("context").is_some() {
        bail!("Studio context is supplied by the channel session.");
    }
    Ok(())
}

fn describe(error: &anyhow::Error) -> Value {
    let mut entry = json!({"error": crate::app::tail(&format!("{error:#}"), 300)});
    if error.is::<AuthRequired>() {
        entry["action"] =
            "Sign into the server browser; Google requires authentication or channel access."
                .into();
    }
    entry
}

/// Videos whose title or thumbnail was changed through google_api_write in
/// the last two weeks: live experiments worth fresh readings.
async fn experiment_videos(app: &App, channel: &str) -> Result<Vec<String>> {
    let (name, since) = (
        channel.to_owned(),
        (now() - jiff::SignedDuration::from_hours(24 * 14)).to_string(),
    );
    app.db
        .call(move |c| {
            let mut statement = c.prepare(
                "SELECT coalesce(json_extract(parameters, '$.videoId'), json_extract(parameters, '$.body.id'))
                 FROM api_writes WHERE channel = ?1 AND started_at >= ?2 AND status = 'succeeded'
                   AND method IN ('videos.update', 'thumbnails.set')
                 ORDER BY started_at DESC",
            )?;
            let ids = statement
                .query_map(params![name, since], |r| r.get::<_, Option<String>>(0))?
                .filter_map(|r| r.ok().flatten())
                .collect();
            Ok(ids)
        })
        .await
}

/// Collect one channel's Studio readings and return its status.
async fn collect(app: &App, channel: &str, client: &mut Client) -> Result<Value> {
    let account = youtube::mine(app, channel, "snippet,status").await?;
    let channel_id = account["id"].as_str().context("channel id")?.to_owned();
    let monetized = account["status"]["isChannelMonetizationEnabled"]
        .as_bool()
        .unwrap_or(false);
    let public: Vec<String> = youtube::channel_videos(app, channel, true)
        .await?
        .iter()
        .filter(|v| v.privacy == "public")
        .map(|v| v.id.clone())
        .collect();
    let mut targets: Vec<String> = experiment_videos(app, channel).await?;
    targets.extend(public.iter().take(5).cloned());
    let mut seen = BTreeSet::new();
    targets.retain(|id| public.contains(id) && seen.insert(id.clone()));
    targets.truncate(10);

    let mut errors = Vec::new();
    let end = now().to_zoned(crate::app::sydney()).date();
    let start = end.checked_sub(jiff::Span::new().days(70))?;
    match client
        .readings(&channel_id, None, "AUD", Some((start, end)), monetized)
        .await
    {
        Ok((daily, approximate)) => {
            let row = (
                channel.to_owned(),
                channel_id.clone(),
                monetized,
                now().to_string(),
                start.to_string(),
                end.to_string(),
                approximate,
                daily.to_string(),
            );
            app.db
                .call(move |c| {
                    c.execute(
                        "INSERT OR REPLACE INTO studio_channel_readings (channel, channel_id, monetized, observed_at,
                            source, currency, reporting_timezone, start_date, end_date_exclusive, approximate, daily)
                         VALUES (?1, ?2, ?3, ?4, ?5, 'AUD', 'America/Los_Angeles', ?6, ?7, ?8, ?9)",
                        params![row.0, row.1, row.2, row.3, SOURCE, row.4, row.5, row.6, row.7],
                    )?;
                    Ok(())
                })
                .await?;
            client.checkpoint()?;
        }
        Err(error) => {
            tracing::warn!("Studio channel totals unavailable for {channel}: {error:#}");
            let mut entry = describe(&error);
            entry["scope"] = "channel_totals".into();
            errors.push(entry);
        }
    }
    let mut saved = 0;
    for video in targets {
        match client
            .readings(&channel_id, Some(&video), "USD", None, true)
            .await
        {
            Ok((metrics, approximate)) => {
                let row = (
                    channel.to_owned(),
                    video.clone(),
                    now().to_string(),
                    metrics.to_string(),
                    approximate,
                );
                let cutoff = (now() - jiff::SignedDuration::from_hours(24 * 35)).to_string();
                app.db
                    .call(move |c| {
                        c.execute(
                            "INSERT OR REPLACE INTO studio_video_readings (channel, video_id, observed_at, url, period,
                                approximate, currency, metrics, source)
                             VALUES (?1, ?2, ?3, ?4, 'since_published', ?5, 'USD', ?6, ?7)",
                            params![
                                row.0,
                                row.1,
                                row.2,
                                format!("{ORIGIN}/video/{}/analytics/tab-overview/period-default", row.1),
                                row.4,
                                row.3,
                                SOURCE
                            ],
                        )?;
                        c.execute(
                            "DELETE FROM studio_video_readings WHERE channel = ?1 AND observed_at < ?2",
                            params![row.0, cutoff],
                        )?;
                        Ok(())
                    })
                    .await?;
                saved += 1;
                client.checkpoint()?;
            }
            Err(error) => {
                tracing::warn!("Studio readings unavailable for {channel}/{video}: {error:#}");
                let mut entry = describe(&error);
                entry["video_id"] = video.into();
                errors.push(entry);
            }
        }
    }
    Ok(json!({"saved": saved, "errors": errors}))
}

/// Collect every channel (or one); true when everything succeeded.
pub async fn collect_all(app: &App, only: Option<&str>) -> Result<bool> {
    let mut client = Client::new().await?;
    let channels = match only {
        Some(channel) => vec![channel.to_owned()],
        None => app.config.names(),
    };
    let mut ok = true;
    for channel in channels {
        let status = collect(app, &channel, &mut client)
            .await
            .unwrap_or_else(|error| {
                tracing::warn!("Studio collection failed for {channel}: {error:#}");
                json!({"saved": 0, "errors": [describe(&error)]})
            });
        ok &= status["errors"].as_array().is_none_or(Vec::is_empty);
        let row = (
            channel,
            now().to_string(),
            status["saved"].as_i64().unwrap_or(0),
            status["errors"].to_string(),
        );
        app.db
            .call(move |c| {
                c.execute(
                    "INSERT OR REPLACE INTO studio_status (channel, checked_at, saved, errors) VALUES (?1, ?2, ?3, ?4)",
                    params![row.0, row.1, row.2, row.3],
                )?;
                Ok(())
            })
            .await?;
    }
    Ok(ok)
}

/// Studio's own signed-in request service, so Google's client handles
/// session risk and attestation itself.
const REQUEST: &str = r#"async ({channelId, endpoint, payload}) => {
    const app = document.querySelector('ytcp-app');
    for (let i=0; i<300 && (!app?.secureRequestService || app.currentChannelId!==channelId); i++)
        await new Promise(resolve => setTimeout(resolve, 100));
    if (!app?.secureRequestService || app.currentChannelId!==channelId)
        throw new Error('Studio runtime unavailable or wrong channel; no request sent');
    const context = app.attestationManagerNetwork.baseContextFactory();
    if (context.user?.delegationContext?.externalChannelId!==channelId)
        throw new Error('Studio delegation does not match channel; no request sent');
    const version = app.attestationManagerNetwork.attApi.gapiVersion;
    return await Promise.race([app.secureRequestService.makeRequest({
        path: '/' + version + '/' + endpoint, httpMethod: 'POST',
        body: {...payload, context}
    }), new Promise((_, reject) => setTimeout(() => reject(new Error('Studio request timed out; inspect remote state')), 60000))]);
}"#;

/// Run one request inside the browser, in a tab of our own.
async fn execute(channel_id: &str, endpoint: &str, payload: &Value) -> Result<Value> {
    check_endpoint(endpoint, payload)?;
    let http = crate::app::http_client(Duration::from_secs(15))?;
    let browser = cdp::Browser::connect(&http).await?;
    browser
        .with_tab(async |tab| {
            tab.goto(&format!("{ORIGIN}/channel/{channel_id}")).await?;
            let href = tab.evaluate("location.origin").await?;
            if href != ORIGIN {
                bail!("Studio sign-in required; no request sent");
            }
            for _ in 0..600 {
                if tab.evaluate("!!document.querySelector('ytcp-app')").await? == true {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            let arguments =
                json!({"channelId": channel_id, "endpoint": endpoint, "payload": payload});
            let response = tab.evaluate(&format!("({REQUEST})({arguments})")).await?;
            if !response.is_object() || response.get("error").is_some() {
                bail!("Studio runtime returned an error; inspect remote state");
            }
            if !response["responseContext"]["webResponseContextExtensionData"]["challenge"]
                .is_null()
            {
                bail!("Studio runtime requires a challenge; inspect remote state");
            }
            Ok(response)
        })
        .await
}

/// The Studio service's worker: drain queued Studio requests, never replaying
/// one whose outcome a crash or timeout left unknown.
pub async fn serve_requests(app: Arc<App>) -> Result<()> {
    app.db
        .call(|c| {
            c.execute(
                "UPDATE studio_requests SET status = 'unconfirmed', finished_at = ?1,
                    error = 'Studio worker restarted during request; inspect remote state before retrying'
                 WHERE status = 'running'",
                [now().to_string()],
            )?;
            Ok(())
        })
        .await?;
    tracing::info!("Studio request worker ready");
    loop {
        let queued: Vec<(String, String, String, String, String)> = app
            .db
            .call(|c| {
                let mut statement = c.prepare(
                    "SELECT id, channel_id, endpoint, payload, submitted_at FROM studio_requests
                     WHERE status = 'queued' ORDER BY submitted_at",
                )?;
                let rows = statement
                    .query_map([], |r| {
                        Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
                    })?
                    .collect::<rusqlite::Result<_>>()?;
                Ok(rows)
            })
            .await?;
        for (id, channel_id, endpoint, payload, submitted) in queued {
            let age = now().duration_since(crate::app::parse_time(&submitted)?);
            if age.as_secs() > REQUEST_SECONDS {
                finish(
                    &app,
                    &id,
                    Err(anyhow!(
                        "Studio request expired before execution; no request sent"
                    )),
                )
                .await?;
                continue;
            }
            let claimed = {
                let id = id.clone();
                app.db
                    .call(move |c| {
                        Ok(c.execute(
                            "UPDATE studio_requests SET status = 'running', started_at = ?2 WHERE id = ?1 AND status = 'queued'",
                            params![id, now().to_string()],
                        )? == 1)
                    })
                    .await?
            };
            if !claimed {
                continue;
            }
            let payload: Value = serde_json::from_str(&payload)?;
            finish(&app, &id, execute(&channel_id, &endpoint, &payload).await).await?;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

async fn finish(app: &App, id: &str, outcome: Result<Value>) -> Result<()> {
    let id = id.to_owned();
    let outcome = outcome.map(|v| v.to_string()).map_err(|e| format!("{e:#}"));
    app.db
        .call(move |c| {
            match outcome {
                Ok(response) => c.execute(
                    "UPDATE studio_requests SET status = 'responded', response = ?2, finished_at = ?3 WHERE id = ?1",
                    params![id, response, now().to_string()],
                )?,
                Err(error) => c.execute(
                    "UPDATE studio_requests SET status = 'unconfirmed', error = ?2, finished_at = ?3 WHERE id = ?1",
                    params![id, error, now().to_string()],
                )?,
            };
            Ok(())
        })
        .await
}

/// Collect the videos a Studio payload targets; any channel it names must be
/// this one.
fn targets(value: &Value, channel_id: &str, videos: &mut BTreeSet<String>) -> Result<()> {
    match value {
        Value::Object(map) => {
            for (key, child) in map {
                // Masks describe requested fields; their booleans are not targets.
                if ["mask", "videoReadMask", "updateMask"].contains(&key.as_str()) {
                    continue;
                }
                if ["channelId", "externalChannelId"].contains(&key.as_str()) && child != channel_id
                {
                    bail!("Studio requests must stay within this channel.");
                }
                match (key.as_str(), child) {
                    ("videoId" | "encryptedVideoId", Value::String(id)) => {
                        videos.insert(id.clone());
                    }
                    ("videoIds", Value::Array(ids)) => {
                        videos.extend(ids.iter().filter_map(|i| i.as_str().map(str::to_owned)));
                    }
                    _ => {}
                }
                targets(child, channel_id, videos)?;
            }
        }
        Value::Array(items) => {
            for item in items {
                targets(item, channel_id, videos)?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// The channel tool: check every target belongs to this channel, journal the
/// request, hand it to the browser worker and wait for its outcome.
pub async fn request(
    app: &App,
    channel: &str,
    endpoint: &str,
    payload: Value,
    observation: Option<Value>,
) -> Result<Value> {
    check_endpoint(endpoint, &payload)?;
    let channel_id = youtube::mine(app, channel, "id").await?["id"]
        .as_str()
        .context("channel id")?
        .to_owned();
    let mut videos = BTreeSet::new();
    targets(&payload, &channel_id, &mut videos)?;
    let ids: Vec<String> = videos.into_iter().collect();
    let found = youtube::videos(app, channel, &ids, "snippet").await?;
    let owned: BTreeSet<&str> = found
        .iter()
        .filter(|v| v["snippet"]["channelId"] == channel_id.as_str())
        .filter_map(|v| v["id"].as_str())
        .collect();
    if owned.len() != ids.len() {
        bail!("Every target video must belong to this channel.");
    }
    let id = uuid::Uuid::new_v4().simple().to_string();
    let row = (
        id.clone(),
        channel.to_owned(),
        channel_id,
        endpoint.to_owned(),
        payload.to_string(),
        observation.map(|o| o.to_string()),
    );
    app.db
        .call(move |c| {
            c.execute(
                "INSERT INTO studio_requests (id, channel, channel_id, endpoint, payload, observation, status, submitted_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'queued', ?7)",
                params![row.0, row.1, row.2, row.3, row.4, row.5, now().to_string()],
            )?;
            Ok(())
        })
        .await?;
    let deadline = std::time::Instant::now() + Duration::from_secs(REQUEST_SECONDS as u64);
    while std::time::Instant::now() < deadline {
        let state = {
            let id = id.clone();
            app.db
                .call(move |c| {
                    Ok(c.query_row(
                        "SELECT status, response, error FROM studio_requests WHERE id = ?1",
                        [&id],
                        |r| {
                            Ok((
                                r.get::<_, String>(0)?,
                                r.get::<_, Option<String>>(1)?,
                                r.get::<_, Option<String>>(2)?,
                            ))
                        },
                    )?)
                })
                .await?
        };
        match state {
            (status, Some(response), _) if status == "responded" => {
                return Ok(
                    json!({"response": serde_json::from_str::<Value>(&response)?, "audit_id": id}),
                );
            }
            (status, _, error) if status == "unconfirmed" => bail!("{}", error.unwrap_or_default()),
            _ => tokio::time::sleep(Duration::from_millis(250)).await,
        }
    }
    bail!("Studio request {id} unconfirmed; inspect remote state before retrying")
}
