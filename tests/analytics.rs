//! Analytics, counter snapshots, Studio collection, the stats header and the
//! discovery-driven Google API, through real HTTP clients.

mod support;

use std::sync::{Arc, Mutex};

use serde_json::{Value, json};
use support::*;

#[derive(Default)]
struct Studio {
    bootstraps: usize,
    calls: usize,
    wrong_channel: bool,
}

struct Fixture {
    youtube: Mutex<YouTube>,
    studio: Mutex<Studio>,
}

fn studio(fixture: &Fixture, req: &Req) -> Option<Resp> {
    if req.host != "studio.youtube.com" {
        return None;
    }
    let mut state = fixture.studio.lock().unwrap();
    if req.path.starts_with("/channel/") {
        state.bootstraps += 1;
        let version = state.bootstraps.to_string();
        let config = json!({
            "INNERTUBE_API_KEY": "fixture-key",
            "INNERTUBE_CONTEXT_CLIENT_NAME": 62,
            "INNERTUBE_CONTEXT_CLIENT_VERSION": version,
            "INNERTUBE_CONTEXT": {"client": {"clientVersion": version}},
            "DELEGATION_CONTEXT": {"externalChannelId": "channel-owned"},
            "INNERTUBE_CONTEXT_SERIALIZED_DELEGATION_CONTEXT": "fixture-delegation",
            "SESSION_INDEX": 0,
        });
        return Some(Resp::bytes(
            "text/html",
            format!("<script>ytcfg.set({config});</script>").into_bytes(),
        ));
    }
    assert_eq!(req.path, "/youtubei/v1/yta_web/get_cards");
    let query = req.json();
    let monetized = fixture.youtube.lock().unwrap().monetized;
    let totals = query["screenConfig"]["entity"] == json!({"channelId": "channel-owned"});
    if totals {
        let range = &query["screenConfig"]["timePeriod"]["dateIdRange"];
        let day = |v: &Value| {
            jiff::civil::Date::strptime("%Y%m%d", v.as_i64().unwrap().to_string()).unwrap()
        };
        assert_eq!(
            (day(&range["exclusiveEnd"]) - day(&range["inclusiveStart"])).get_days(),
            70
        );
    } else {
        assert_eq!(
            query["screenConfig"]["entity"],
            json!({"videoId": "owned-video"})
        );
        assert_eq!(
            query["screenConfig"]["timePeriod"]["timePeriodType"],
            "ANALYTICS_TIME_PERIOD_TYPE_SINCE_PUBLISH"
        );
    }
    let requested: Vec<String> = query["cardConfigs"][0]["keyMetricCardConfig"]["metricTabConfigs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["metric"].as_str().unwrap().to_owned())
        .collect();
    if totals {
        assert_eq!(
            requested.contains(&"TOTAL_ESTIMATED_EARNINGS".to_owned()),
            monetized
        );
    }
    assert_eq!(
        query["screenConfig"]["currency"],
        if totals { "AUD" } else { "USD" }
    );
    assert_eq!(
        query["context"]["user"]["delegationContext"]["externalChannelId"],
        "channel-owned"
    );
    assert_eq!(
        query["context"]["user"]["serializedDelegationContext"],
        "fixture-delegation"
    );
    assert_eq!(
        req.header("x-youtube-client-version"),
        state.bootstraps.to_string()
    );
    assert_eq!(
        req.header("x-youtube-delegation-context"),
        "fixture-delegation"
    );
    assert!(req.header("cookie").contains("SAPISID=fixture-session"));
    let authorization = req.header("authorization");
    let (stamp, digest) = authorization
        .strip_prefix("SAPISIDHASH ")
        .unwrap()
        .split_once('_')
        .unwrap();
    assert!((jiff::Timestamp::now().as_second() - stamp.parse::<i64>().unwrap()).abs() < 10);
    use sha1::Digest;
    assert_eq!(
        digest,
        hex::encode(sha1::Sha1::digest(format!(
            "{stamp} fixture-session https://studio.youtube.com"
        )))
    );
    state.calls += 1;
    if state.calls == 1 {
        return Some(Resp::status(401, json!({"error": {"code": 401}})));
    }
    let values = [
        ("EXTERNAL_VIEWS", 9401.0),
        ("EXTERNAL_WATCH_TIME", 1275840000.0),
        ("SUBSCRIBERS_NET_CHANGE", -2.0),
        (
            "TOTAL_ESTIMATED_EARNINGS",
            if totals { 3949.0 } else { 2468.0 },
        ),
        ("VIDEO_THUMBNAIL_IMPRESSIONS", 34567.0),
        ("VIDEO_THUMBNAIL_IMPRESSIONS_VTR", 6.04),
    ];
    let yesterday = jiff::Timestamp::now()
        .to_zoned(jiff::tz::TimeZone::get("Australia/Sydney").unwrap())
        .date()
        .yesterday()
        .unwrap();
    let stamp = yesterday
        .to_zoned(jiff::tz::TimeZone::get("America/Los_Angeles").unwrap())
        .unwrap()
        .timestamp()
        .as_millisecond();
    let tabs: Vec<Value> = values
        .iter()
        .filter(|(metric, _)| requested.contains(&metric.to_string()))
        .map(|(metric, value)| {
            let mut content = json!({"metric": metric, "total": value, "anomalies": [{"type": "ANALYTICS_ANOMALY_TYPE_DATA_ESTIMATED"}]});
            if totals {
                content["mainSeries"] = json!({"datums": [{"x": stamp, "y": value}], "timeUnit": "TIME_PERIOD_UNIT_DAYS"});
                if *metric == "TOTAL_ESTIMATED_EARNINGS" {
                    content["mainSeries"]["datums"] = json!([{"x": stamp - 86400000, "y": value}, {"x": stamp, "y": 0}]);
                    content["anomalies"] = json!([{"impact": "ANALYTICS_ANOMALY_IMPACT_DATA_MISSING", "startDate": stamp, "endDate": stamp + 86400000}]);
                }
            }
            json!({"primaryContent": content})
        })
        .collect();
    let channel = if state.wrong_channel {
        "wrong-channel"
    } else {
        "channel-owned"
    };
    Some(
        Resp::json(json!({"cards": [{"config": {"topEntitySelection": {"channelId": channel}}, "keyMetricCardData": {"keyMetricTabs": tabs}}]}))
            .with_header("set-cookie", "SIDCC=rotated; Domain=.youtube.com; Path=/; Secure; HttpOnly"),
    )
}

fn google(req: &Req) -> Option<Resp> {
    match req.host.as_str() {
        "youtubeanalytics.googleapis.com" => {
            let metrics: Vec<String> = req
                .arg("metrics")
                .unwrap()
                .split(',')
                .map(str::to_owned)
                .collect();
            assert!(
                !(metrics.contains(&"estimatedRevenue".into())
                    && metrics.contains(&"views".into()))
            );
            let lag = if metrics.contains(&"estimatedRevenue".into()) {
                4
            } else {
                2
            };
            let day = jiff::Timestamp::now()
                .to_zoned(jiff::tz::TimeZone::UTC)
                .date()
                .checked_sub(jiff::Span::new().days(lag))
                .unwrap();
            let dimension = req.arg("dimensions").unwrap_or_default();
            let value = |m: &str| match m {
                "views" => json!(42),
                "estimatedRevenue" => json!(1.25),
                "estimatedMinutesWatched" => json!(12),
                _ => json!(0),
            };
            let mut row = vec![if dimension == "day" {
                json!(day.to_string())
            } else {
                json!("owned-video")
            }];
            row.extend(metrics.iter().map(|m| value(m)));
            let mut headers = vec![json!({"name": dimension})];
            headers.extend(metrics.iter().map(|m| json!({"name": m})));
            Some(Resp::json(json!({"columnHeaders": headers, "rows": [row]})))
        }
        "youtubereporting.googleapis.com" if req.path.starts_with("/v1/media/") => {
            assert_eq!(req.arg("alt").as_deref(), Some("media"));
            Some(Resp::bytes(
                "text/csv",
                b"date,video_id,video_thumbnail_impressions\n20261020,owned-video,34567\n".to_vec(),
            ))
        }
        "i.ytimg.com" if req.path == "/max.jpg" => Some(Resp::status(404, json!({}))),
        "i.ytimg.com" => Some(Resp::bytes(
            "image/jpeg",
            std::fs::read(format!("{ROOT}/thumbnails/blow.jpg")).unwrap(),
        )),
        _ => None,
    }
}

fn setup(monetized: bool) -> (tokio::runtime::Runtime, Providers, Root, Arc<Fixture>) {
    let runtime = runtime();
    let now = jiff::Timestamp::now();
    let published = (now - jiff::SignedDuration::from_hours(6 * 24)).to_string();
    let mut youtube = YouTube {
        monetized,
        subscribers: 11200,
        ..Default::default()
    };
    youtube.videos = vec![video(
        "owned-video",
        "public",
        &published,
        json!({"snippet": {"thumbnails": {"maxres": {"url": "https://i.ytimg.com/max.jpg"}, "high": {"url": "https://i.ytimg.com/high.jpg"}}}}),
    )];
    let fixture = Arc::new(Fixture {
        youtube: Mutex::new(youtube),
        studio: Mutex::new(Studio::default()),
    });
    let handler = {
        let fixture = fixture.clone();
        Arc::new(move |req: Req| {
            discovery(&req)
                .or_else(|| YouTube::handle(&fixture.youtube, &req))
                .or_else(|| studio(&fixture, &req))
                .or_else(|| google(&req))
                .or_else(|| openrouter(&req))
                .unwrap_or_else(|| {
                    Resp::status(
                        404,
                        json!({"error": format!("no fake for {}{}", req.host, req.path)}),
                    )
                })
        })
    };
    let providers = providers(&runtime, handler);
    let root = Root::new(CONFIG.split("[channels.science]").next().unwrap());
    root.token("blow");
    // Yesterday's counters, as the hourly sampler would have recorded them.
    let observed = (now - jiff::SignedDuration::from_hours(24)).to_string();
    root.db()
        .with(move |c| {
            c.execute("INSERT INTO counter_snapshots VALUES ('blow', ?1, 11100)", [&observed])?;
            c.execute(
                "INSERT INTO counter_videos VALUES ('blow', ?1, 'owned-video', 'Example', 'public', ?2, 100, 5, 1)",
                [&observed, &published],
            )?;
            Ok(())
        })
        .unwrap();
    (runtime, providers, root, fixture)
}

fn collect(root: &Root, providers: &Providers, session: &std::path::Path) -> std::process::Output {
    command(root, Some(providers), &["collect", "blow"])
        .env("CLIPY_STUDIO_SESSION", session)
        // No browser is running; direct collection never needs one.
        .env(
            "CLIPY_STUDIO_CDP",
            format!("http://127.0.0.1:{}", free_port()),
        )
        .output()
        .unwrap()
}

fn analytics(metrics: &str) -> Value {
    let today = jiff::Timestamp::now()
        .to_zoned(jiff::tz::TimeZone::UTC)
        .date();
    json!({
        "ids": "channel==MINE", "dimensions": "day", "sort": "day", "metrics": metrics,
        "startDate": today.checked_sub(jiff::Span::new().days(28)).unwrap().to_string(),
        "endDate": today.to_string(),
    })
}

fn collect_report_and_tune(monetized: bool) {
    let (runtime, providers, root, fixture) = setup(monetized);
    let server = Server::start(&root, Some(&providers), &[]);
    let base = server.base.clone();
    let stats = |base: &str| {
        http(
            &format!("{base}/stats"),
            "GET",
            Some(&format!("Bearer {TOKEN}")),
        )
        .unwrap()
        .1
    };
    runtime.block_on(async {
        let mut reports = Vec::new();
        for metrics in ["views,estimatedMinutesWatched", "estimatedRevenue"] {
            let args = json!({"api": "youtubeAnalytics", "version": "v2", "method": "reports.query", "parameters": analytics(metrics)});
            reports.push(data(&call(&base, "blow", "google_api_read", args).await));
        }
        assert!(reports[0]["rows"][0][0].as_str().unwrap() > reports[1]["rows"][0][0].as_str().unwrap());
        let history = data(&call(&base, "blow", "read_observations", json!({})).await)["snapshots"].clone();
        assert!(history[0]["videos"]["owned-video"]["views"].as_i64() > history[1]["videos"]["owned-video"]["views"].as_i64());
        // Private uploads often lack max-resolution artwork; another rendition still previews.
        let preview = image(&call(&base, "blow", "view_thumbnail", json!({"video_id": "owned-video"})).await);
        assert_eq!((preview.width(), preview.height()), (1920, 1080));
    });

    let session = root.path().join("private-session.json");
    let cookie = |name: &str, domain: &str| json!({"name": name, "value": "fixture-session", "domain": domain, "path": "/", "secure": true, "httpOnly": true, "expires": -1});
    std::fs::write(
        &session,
        json!([
            cookie("SID", ".google.com"),
            cookie("SAPISID", ".youtube.com")
        ])
        .to_string(),
    )
    .unwrap();
    let output = collect(&root, &providers, &session);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    runtime.block_on(async {
        let observations = data(&call(&base, "blow", "read_observations", json!({})).await);
        let channel = &observations["studio"]["channel"];
        assert_eq!(channel["currency"], "AUD");
        assert_eq!(channel["reporting_timezone"], "America/Los_Angeles");
        assert_eq!(channel["stale"], false);
        assert!(channel["daily"].as_object().unwrap().values().any(|d| d["impressions"] == 34567.0));
        let reading = &observations["studio"]["videos"][0];
        assert_eq!(reading["metrics"]["watch_hours"], 354.4);
        assert_eq!(reading["metrics"]["ctr_percent"], 6.04);
        let csv = text(
            &call(
                &base,
                "blow",
                "google_api_read",
                json!({"api": "youtubereporting", "version": "v1", "method": "media.download",
                       "parameters": {"resourceName": "CHANNEL/channel-owned/jobs/reach/reports/report", "alt": "media"}}),
            )
            .await,
        );
        assert!(csv.contains("owned-video,34567"));
    });
    let header = stats(&base);
    assert!(
        header.contains("9,401") && header.contains("-2"),
        "{header}"
    );
    assert_eq!(header.contains("A$3.95"), monetized, "{header}");
    assert!(header.contains("Earnings (AUD)"));
    assert!(!header.contains("$2.47"));
    let snapshot = root
        .db()
        .with(|c| Ok(clipy::observations::channel_reading(c, "blow")?.unwrap()))
        .unwrap();
    let daily = snapshot["daily"].as_object().unwrap();
    let latest = daily.keys().max().unwrap();
    assert!(daily[latest]["revenue_aud"].is_null());
    assert!(daily.values().all(|d| d.get("revenue_usd").is_none()));
    let saved: Vec<Value> =
        serde_json::from_str(&std::fs::read_to_string(&session).unwrap()).unwrap();
    assert!(
        saved
            .iter()
            .any(|c| c["name"] == "SIDCC" && c["value"] == "rotated")
    );
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        std::fs::metadata(&session).unwrap().permissions().mode() & 0o777,
        0o600
    );

    // A title experiment journals its intent, baseline and outcome.
    runtime.block_on(async {
        let read = json!({"api": "youtube", "version": "v3", "method": "videos.list", "parameters": {"part": "snippet", "id": "owned-video"}});
        let current = data(&call(&base, "blow", "google_api_read", read.clone()).await)["items"][0].clone();
        let mut snippet = current["snippet"].clone();
        snippet["title"] = "A better title".into();
        let body = json!({"id": current["id"], "snippet": snippet});
        let baseline = data(&call(&base, "blow", "read_observations", json!({})).await)["snapshots"][0].clone();
        let mut write = read.clone();
        write["method"] = "videos.update".into();
        write["parameters"] = json!({"part": "snippet", "body": body});
        write["observation"] = json!({"before": current, "baseline": baseline});
        let result = data(&call(&base, "blow", "google_api_write", write).await);
        let verified = data(&call(&base, "blow", "google_api_read", read).await)["items"][0].clone();
        assert_eq!(verified["snippet"], body["snippet"]);
        let id = result["audit_id"].as_str().unwrap().to_owned();
        let journal = root
            .db()
            .with(move |c| {
                Ok(c.query_row("SELECT observation, parameters, started_at <= finished_at FROM api_writes WHERE id = ?1", [&id], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, bool>(2)?))
                })?)
            })
            .unwrap();
        let observation: Value = serde_json::from_str(&journal.0).unwrap();
        assert_eq!(observation["before"], current);
        assert_eq!(observation["baseline"], baseline);
        assert_eq!(serde_json::from_str::<Value>(&journal.1).unwrap()["body"], body);
        assert!(journal.2);
    });

    // A wrong channel must not overwrite good readings; old readings never count as fresh.
    fixture.studio.lock().unwrap().wrong_channel = true;
    assert!(!collect(&root, &providers, &session).status.success());
    let unchanged = root
        .db()
        .with(|c| Ok(clipy::observations::channel_reading(c, "blow")?.unwrap()))
        .unwrap();
    assert_eq!(unchanged, snapshot);
    root.db()
        .with(|c| Ok(c.execute("UPDATE studio_channel_readings SET currency = 'USD'", [])?))
        .unwrap();
    let old_currency = stats(&base);
    assert!(
        !old_currency.contains("A$3.95") && old_currency.contains("fresh AUD collection"),
        "{old_currency}"
    );
    let stale = (jiff::Timestamp::now() - jiff::SignedDuration::from_hours(3)).to_string();
    root.db()
        .with(move |c| {
            Ok(c.execute(
                "UPDATE studio_channel_readings SET currency = 'AUD', observed_at = ?1",
                [&stale],
            )?)
        })
        .unwrap();
    assert!(!stats(&base).contains("9,401"));
}

#[test]
fn collect_report_and_tune_monetized_channel() {
    collect_report_and_tune(true);
}

#[test]
fn collect_report_and_tune_unmonetized_channel() {
    collect_report_and_tune(false);
}

#[test]
fn discovered_api_methods_preserve_raw_data_and_separate_writes() {
    let (runtime, providers, root, _fixture) = setup(true);
    let server = Server::start(&root, Some(&providers), &[]);
    let base = server.base.clone();
    let journal = |root: &Root| {
        root.db()
            .with(|c| {
                Ok(c.query_row("SELECT count(*) FROM api_writes", [], |r| {
                    r.get::<_, i64>(0)
                })?)
            })
            .unwrap()
    };
    runtime.block_on(async {
        let mut matches = Vec::new();
        let mut offset = Some(0);
        while let Some(at) = offset {
            let page = data(&call(&base, "blow", "google_api_spec", json!({"query": "videos.update", "limit": 1, "offset": at})).await);
            matches.extend(page["matches"].as_array().unwrap().clone());
            offset = page["next_offset"].as_u64();
        }
        let pointer = matches
            .iter()
            .map(|m| m["pointer"].as_str().unwrap())
            .find(|p| p.ends_with("/id"))
            .unwrap()
            .rsplit_once('/')
            .unwrap()
            .0
            .to_owned();
        let method = data(&call(&base, "blow", "google_api_spec", json!({"pointer": pointer, "query": null})).await)["value"].clone();
        // The executable path comes from Google's spec, not a server-side catalog.
        let operation = method["id"].as_str().unwrap().split('.').skip(1).collect::<Vec<_>>().join(".");
        let read = json!({"api": "youtube", "version": "v3", "method": "videos.list", "parameters": {"part": "snippet", "id": "owned-video"}});
        let before = data(&call(&base, "blow", "google_api_read", read.clone()).await)["items"].clone();
        let mut snippet = before[0]["snippet"].clone();
        snippet["title"] = "An experiment".into();
        let body = json!({"id": before[0]["id"], "snippet": snippet});
        let mut mutation = read.clone();
        mutation["method"] = operation.into();
        mutation["parameters"] = json!({"part": "snippet", "body": body});
        assert!(is_error(&call(&base, "blow", "google_api_read", mutation.clone()).await));
        assert!(is_error(&call(&base, "blow", "google_api_write", read.clone()).await));
        assert_eq!(data(&call(&base, "blow", "google_api_read", read.clone()).await)["items"], before);
        let mut unknown = read.clone();
        unknown["method"] = "videos.not_a_published_method".into();
        assert!(is_error(&call(&base, "blow", "google_api_read", unknown).await));
        assert_eq!(journal(&root), 0);
        let changed = data(&call(&base, "blow", "google_api_write", mutation).await);
        assert_eq!(data(&call(&base, "blow", "google_api_read", read.clone()).await)["items"][0]["snippet"], body["snippet"]);
        let id = changed["audit_id"].as_str().unwrap().to_owned();
        let response = root
            .db()
            .with(move |c| Ok(c.query_row("SELECT response FROM api_writes WHERE id = ?1", [&id], |r| r.get::<_, String>(0))?))
            .unwrap();
        assert_eq!(serde_json::from_str::<Value>(&response).unwrap(), changed["response"]);
        // Raw service errors are kept and the mutation stays unconfirmed.
        let mut failing = read.clone();
        failing["method"] = "comments.insert".into();
        failing["parameters"] = json!({"part": "snippet", "body": {"snippet": {"textOriginal": "test"}}});
        assert!(is_error(&call(&base, "blow", "google_api_write", failing).await));
        let unconfirmed = root
            .db()
            .with(|c| Ok(c.query_row("SELECT count(*) FROM api_writes WHERE status = 'unconfirmed' AND error IS NOT NULL", [], |r| r.get::<_, i64>(0))?))
            .unwrap();
        assert_eq!(unconfirmed, 1);
        // Media uploads only from this channel's own files.
        let outside = root.path().join("outside.jpg");
        std::fs::copy(format!("{ROOT}/thumbnails/blow.jpg"), &outside).unwrap();
        let mut media = read.clone();
        media["method"] = "thumbnails.set".into();
        media["parameters"] = json!({"videoId": "owned-video"});
        media["media_path"] = json!(outside);
        assert!(is_error(&call(&base, "blow", "google_api_write", media.clone()).await));
        let token = root.path().join("channels/blow/youtube-token.json");
        media["media_path"] = json!(token);
        assert!(is_error(&call(&base, "blow", "google_api_write", media.clone()).await));
        // Artwork drawn for a live video comes back as an image to inspect,
        // with the server path to upload.
        let drawn = call(&base, "blow", "draw_thumbnail", json!({"thumbnail_prompt": "a chip in chains"})).await;
        let artwork = image(&drawn);
        assert_eq!((artwork.width(), artwork.height()), (1920, 1080));
        let inside = std::path::PathBuf::from(data(&drawn)["path"].as_str().unwrap());
        assert!(inside.starts_with(root.path().join("channels/blow/artwork")));
        media["media_path"] = json!(inside);
        let uploaded = data(&call(&base, "blow", "google_api_write", media).await);
        let id = uploaded["audit_id"].as_str().unwrap().to_owned();
        let path = root
            .db()
            .with(move |c| Ok(c.query_row("SELECT media_path FROM api_writes WHERE id = ?1", [&id], |r| r.get::<_, String>(0))?))
            .unwrap();
        assert_eq!(std::fs::canonicalize(path).unwrap(), std::fs::canonicalize(&inside).unwrap());
    });
}
