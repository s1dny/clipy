//! Enable ads through Studio's own request service in a real browser, and
//! surface authentication challenges, through the channel MCP server.

mod support;

use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use support::*;

const PAGE: &str = r#"<ytcp-app></ytcp-app><script>
customElements.define('ytcp-app', class extends HTMLElement {
  connectedCallback() {
    this.currentChannelId = 'channel-owned';
    this.attestationManagerNetwork = {attApi: {gapiVersion: 'youtubei/v1'},
      baseContextFactory: () => ({user: {delegationContext: {externalChannelId: 'channel-owned'}}})};
    this.secureRequestService = {makeRequest: async r => (await fetch(r.path, {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Fixture-Studio-Proof': 'native-runtime'},
      body: JSON.stringify(r.body)})).json()};
  }
});
</script>"#;

#[derive(Default)]
struct Studio {
    ads_on: bool,
    challenge: bool,
    writes: usize,
}

fn studio(state: &Mutex<Studio>, req: &Req) -> Option<Resp> {
    if req.host != "studio.youtube.com" {
        return None;
    }
    if req.path == "/channel/channel-owned" {
        return Some(Resp::bytes("text/html", PAGE.as_bytes().to_vec()));
    }
    if !req.path.ends_with("/get_creator_videos") && !req.path.ends_with("/metadata_update") {
        return Some(Resp::status(404, json!({})));
    }
    assert_eq!(req.header("x-fixture-studio-proof"), "native-runtime");
    let body = req.json();
    assert_eq!(
        body["context"]["user"]["delegationContext"]["externalChannelId"],
        "channel-owned"
    );
    let mut state = state.lock().unwrap();
    if req.path.ends_with("/metadata_update") {
        state.writes += 1;
        assert_eq!(body["encryptedVideoId"], "owned-video");
        if state.challenge {
            return Some(Resp::json(
                json!({"responseContext": {"webResponseContextExtensionData": {
                "challenge": {"type": "CHALLENGE_PROMPT_TYPE_AUTHENTICATE"}}}}),
            ));
        }
        state.ads_on = body["monetizationSettings"]["newMonetizeWithAds"] == true;
        return Some(Resp::json(
            json!({"monetizationSettings": {"success": true}}),
        ));
    }
    assert_eq!(body["videoIds"], json!(["owned-video"]));
    let (set, effective) = if state.ads_on {
        (
            "VIDEO_USER_SET_MONETIZATION_ON",
            "VIDEO_MONETIZING_STATUS_MONETIZING",
        )
    } else {
        (
            "VIDEO_USER_SET_MONETIZATION_OFF",
            "VIDEO_MONETIZING_STATUS_NOT_MONETIZING_OFF",
        )
    };
    Some(Resp::json(
        json!({"videos": [{"videoId": "owned-video", "channelId": "channel-owned",
        "monetization": {"adMonetization": {"userSetMonetization": set, "effectiveStatus": effective}}}]}),
    ))
}

fn chrome() -> String {
    let candidates = [
        std::env::var("CLIPY_TEST_CHROME").unwrap_or_default(),
        "/Applications/Chromium.app/Contents/MacOS/Chromium".into(),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome".into(),
        "/usr/bin/chromium".into(),
        "/usr/bin/google-chrome".into(),
    ];
    candidates
        .into_iter()
        .find(|c| !c.is_empty() && std::path::Path::new(c).exists())
        .expect("Chrome or Chromium is required; set CLIPY_TEST_CHROME")
}

struct Killed(Child);

impl Drop for Killed {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn ads_write_requires_verified_readback_and_surfaces_authentication() {
    let runtime = runtime();
    let state = Arc::new(Mutex::new(Studio::default()));
    let youtube = Arc::new(Mutex::new(YouTube::default()));
    youtube.lock().unwrap().videos = vec![video(
        "owned-video",
        "public",
        "2026-10-01T00:00:00Z",
        json!({}),
    )];
    let handler = {
        let (state, youtube) = (state.clone(), youtube.clone());
        Arc::new(move |req: Req| {
            studio(&state, &req)
                .or_else(|| YouTube::handle(&youtube, &req))
                .unwrap_or_else(|| Resp::status(404, json!({})))
        })
    };
    let providers = providers(&runtime, handler);
    let root = Root::new(CONFIG.split("[channels.science]").next().unwrap());
    root.token("blow");
    let port = free_port();
    let profile = root.path().join("browser");
    let _browser = Killed(
        Command::new(chrome())
            .args([
                "--headless=new",
                "--no-sandbox",
                "--no-first-run",
                "--disable-background-networking",
                "--ignore-certificate-errors",
                &format!("--remote-debugging-port={port}"),
                &format!("--user-data-dir={}", profile.display()),
                &format!("--proxy-server={}", providers.url()),
                "about:blank",
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(30);
    while ureq_get(&format!("http://127.0.0.1:{port}/json/version")).is_none() {
        assert!(Instant::now() < deadline, "browser did not start");
        std::thread::sleep(Duration::from_millis(100));
    }
    let cdp = format!("http://127.0.0.1:{port}");
    let server = Server::start(&root, Some(&providers), &[]);
    let worker_log = std::fs::File::create(root.path().join("worker.log")).unwrap();
    let _worker = Killed(
        command(&root, Some(&providers), &["studio"])
            .env("CLIPY_STUDIO_CDP", &cdp)
            .stdout(worker_log.try_clone().unwrap())
            .stderr(worker_log)
            .spawn()
            .unwrap(),
    );
    let base = server.base.as_str();
    let audits = |status: &str| -> Vec<Value> {
        let status = status.to_owned();
        root.db()
            .with(move |c| {
                let mut statement =
                    c.prepare("SELECT observation FROM studio_requests WHERE status = ?1")?;
                let rows = statement
                    .query_map([&status], |r| r.get::<_, Option<String>>(0))?
                    .map(|r| {
                        r.map(|o| {
                            o.and_then(|o| serde_json::from_str(&o).ok())
                                .unwrap_or(Value::Null)
                        })
                    })
                    .collect::<rusqlite::Result<_>>()?;
                Ok(rows)
            })
            .unwrap()
    };
    runtime.block_on(async {
        let read = json!({"endpoint": "creator/get_creator_videos", "payload": {
            "videoIds": ["owned-video"], "mask": {"videoId": true, "channelId": true, "monetization": {"all": true}}, "criticalRead": true}});
        let first = call(base, "blow", "studio_api_request", read.clone()).await;
        let before = data(&first)["response"]["videos"][0].clone();
        assert!(before["monetization"]["adMonetization"]["userSetMonetization"].as_str().unwrap().ends_with("_OFF"));
        let write = json!({"endpoint": "video_manager/metadata_update",
            "payload": {"encryptedVideoId": "owned-video", "monetizationSettings": {"newMonetizeWithAds": true}},
            "observation": before});
        state.lock().unwrap().challenge = true;
        let failure = call(base, "blow", "studio_api_request", write.clone()).await;
        assert!(is_error(&failure) && text(&failure).contains("challenge"), "{}", text(&failure));
        assert_eq!(state.lock().unwrap().writes, 1);
        assert!(!state.lock().unwrap().ads_on);
        assert!(audits("unconfirmed").contains(&before));
        assert!(!is_error(&call(base, "blow", "studio_api_request", read.clone()).await));
        state.lock().unwrap().challenge = false;
        data(&call(base, "blow", "studio_api_request", write.clone()).await);
        let after = data(&call(base, "blow", "studio_api_request", read).await)["response"]["videos"][0].clone();
        assert_eq!(after["monetization"]["adMonetization"]["effectiveStatus"], "VIDEO_MONETIZING_STATUS_MONETIZING");
        let mut foreign = write.clone();
        foreign["payload"]["encryptedVideoId"] = "another-video".into();
        let denied = call(base, "blow", "studio_api_request", foreign).await;
        assert!(is_error(&denied));
        assert_eq!(state.lock().unwrap().writes, 2);
    });
}
