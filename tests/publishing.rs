//! Publish, reconcile a lost response and repair a broken release over MCP.

mod support;

use std::sync::{Arc, Mutex};

use serde_json::{Value, json};
use support::*;

const FAKE_YT_DLP: &str = r#"#!/bin/sh
out=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; esac
  shift
done
target=$(printf '%s' "$out" | sed 's/%(ext)s/mkv/')
cp "$FAKE_MEDIA" "$target"
printf '%s\n' "$target"
"#;

struct Setup {
    runtime: tokio::runtime::Runtime,
    providers: Providers,
    root: Root,
    youtube: Arc<Mutex<YouTube>>,
    raw: std::path::PathBuf,
    publish_at: String,
}

fn source() -> Value {
    json!({"name": "Fixture stream", "streamed_on": "2020-01-02", "evidence": "Fixture broadcast metadata"})
}

fn plan() -> Value {
    json!({
        "title": "Clip 01", "description": "A clip.", "thumbnail_prompt": "a compiler",
        "start_ms": 0, "end_ms": 500000, "source": source(),
        "keep": [
            {"start_ms": 0, "end_ms": 200000, "reason": "Opening argument"},
            {"start_ms": 210000, "end_ms": 400000, "reason": "Skip a donation alert"},
            {"start_ms": 410000, "end_ms": 500000, "reason": "Skip a false start"},
        ],
        "annotations": [
            {"start_ms": 0, "end_ms": 5000, "label": "Context", "heading": "Clip 01", "text": "What the clip is about.", "evidence": "Fixture"},
            {"start_ms": 60000, "end_ms": 70000, "label": "Reference", "heading": "A compiler", "text": "What a compiler is.", "evidence": "Fixture"},
        ],
    })
}

fn setup(status: &str) -> Setup {
    let runtime = runtime();
    let youtube = Arc::new(Mutex::new(YouTube::default()));
    let handler = {
        let youtube = youtube.clone();
        Arc::new(move |req: Req| {
            YouTube::handle(&youtube, &req)
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
    let root = Root::new(CONFIG);
    root.token("talks");
    let raw = root.path().join("raw.mkv");
    ffmpeg(&[
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:size=320x180:rate=1/10:duration=500",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=8000:cl=mono",
        "-t",
        "500",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "1",
        "-c:a",
        "pcm_s16le",
        raw.to_str().unwrap(),
    ]);
    let mut clip = clip_row(
        &root,
        "talks",
        "aaaaaaaaaaa",
        1,
        status,
        Some("Example Speaker"),
    );
    let selection: clipy::clips::ClipSelection = serde_json::from_value(plan()).unwrap();
    let output = root
        .path()
        .join("channels/talks/clips/aaaaaaaaaaa-01/video.mp4");
    let http = clipy::app::http_client(std::time::Duration::from_secs(30)).unwrap();
    let report = runtime
        .block_on(clipy::render::render(
            &raw,
            &raw,
            &selection,
            &output,
            &root.path().join("assets"),
            &http,
            "ffmpeg",
        ))
        .unwrap();
    let publish_at = (jiff::Timestamp::now() + jiff::SignedDuration::from_hours(48))
        .strftime("%Y-%m-%dT%H:%M:%SZ")
        .to_string();
    clip.start_ms = 1300;
    clip.duration_ms = 480000;
    clip.source = Some(serde_json::from_value(source()).unwrap());
    clip.edit_report = Some(report);
    clip.publish_at = Some(publish_at.clone());
    let saved = clip.clone();
    root.db().with(move |c| saved.save(c)).unwrap();
    let plans = json!([plan()]).to_string();
    root.db()
        .with(move |c| {
            c.execute(
                "INSERT INTO selections VALUES ('talks', 'aaaaaaaaaaa', 'Example Speaker', ?1, '2026-10-30T00:00:00Z')",
                [plans],
            )?;
            Ok(())
        })
        .unwrap();
    root.tool("yt-dlp", FAKE_YT_DLP);
    Setup {
        runtime,
        providers,
        root,
        youtube,
        raw,
        publish_at,
    }
}

fn row(root: &Root) -> clipy::queue::Clip {
    root.db()
        .with(|c| clipy::queue::get(c, "talks", "aaaaaaaaaaa:01"))
        .unwrap()
}

#[test]
fn scheduled_upload_survives_reset_and_lost_confirmation() {
    let setup = setup("pending");
    setup.youtube.lock().unwrap().reset = true;
    let server = Server::start(&setup.root, Some(&setup.providers), &[]);
    let base = server.base.as_str();
    let media = setup
        .root
        .path()
        .join("channels/talks/clips/aaaaaaaaaaa-01/video.mp4");
    setup.runtime.block_on(async {
        let job = data(
            &call(
                base,
                "talks",
                "publish_clip",
                json!({"clip_id": "aaaaaaaaaaa:01", "publish_at": setup.publish_at}),
            )
            .await,
        );
        let job = wait_job(base, "talks", &job).await;
        assert_eq!(job["status"], "succeeded", "{job}\n{}", server.log());
        {
            let youtube = setup.youtube.lock().unwrap();
            assert_eq!((youtube.probes, youtube.sessions), (1, 1));
            assert_eq!(youtube.inserted["status"]["privacyStatus"], "private");
            assert_eq!(youtube.thumbnails, 1);
            let description = youtube.inserted["snippet"]["description"].as_str().unwrap();
            assert!(
                description.contains(
                    "Source: Fixture stream https://youtube.com/watch?v=aaaaaaaaaaa&t=1s"
                ),
                "{description}"
            );
            assert!(description.contains("Streamed: 2020-01-02"));
        }
        let saved = row(&setup.root);
        assert_eq!(saved.youtube_id.as_deref(), Some("new-upload"));
        assert_eq!(saved.publish_at.as_deref(), Some(setup.publish_at.as_str()));
        // The provider succeeded but the local confirmation was lost.
        setup
            .root
            .db()
            .with(|c| Ok(c.execute("UPDATE clips SET status = 'failed', youtube_id = NULL", [])?))
            .unwrap();
        let job = data(
            &call(
                base,
                "talks",
                "publish_clip",
                json!({"clip_id": "aaaaaaaaaaa:01"}),
            )
            .await,
        );
        assert_eq!(wait_job(base, "talks", &job).await["status"], "succeeded");
        let youtube = setup.youtube.lock().unwrap();
        assert_eq!((youtube.sessions, youtube.videos.len()), (1, 1));
        assert_eq!(row(&setup.root).youtube_id.as_deref(), Some("new-upload"));
        assert!(!media.exists());
    });
}

#[test]
fn publishing_refuses_a_pending_clip_below_the_video_format() {
    let setup = setup("pending");
    let mut old = plan();
    old["keep"] = json!([]);
    old["annotations"] = json!([]);
    old["end_ms"] = 480000.into();
    let plans = json!([old]).to_string();
    setup
        .root
        .db()
        .with(move |c| Ok(c.execute("UPDATE selections SET clips = ?1", [plans])?))
        .unwrap();
    let server = Server::start(&setup.root, Some(&setup.providers), &[]);
    let base = server.base.as_str();
    setup.runtime.block_on(async {
        let job = data(
            &call(
                base,
                "talks",
                "publish_clip",
                json!({"clip_id": "aaaaaaaaaaa:01", "publish_at": setup.publish_at}),
            )
            .await,
        );
        let job = wait_job(base, "talks", &job).await;
        assert_eq!(job["status"], "failed", "{job}");
        assert!(job.to_string().contains("edit_video"), "{job}");
        assert_eq!(setup.youtube.lock().unwrap().sessions, 0);
        assert_eq!(row(&setup.root).status, "pending");
    });
}

#[test]
fn repair_preserves_release_and_never_replaces_public_video() {
    let setup = setup("uploaded");
    let publish_at = setup.publish_at.clone();
    setup
        .root
        .db()
        .with(|c| Ok(c.execute("UPDATE clips SET youtube_id = 'broken-upload'", [])?))
        .unwrap();
    setup.youtube.lock().unwrap().videos = vec![json!({
        "id": "broken-upload",
        "snippet": {"title": "Clip 01", "categoryId": "28", "description": "Original: https://youtube.com/watch?v=aaaaaaaaaaa",
                    "publishedAt": "2026-10-05T00:00:00Z"},
        "status": {"privacyStatus": "public", "uploadStatus": "uploaded", "publishAt": publish_at,
                   "embeddable": true, "publicStatsViewable": true},
        "processingDetails": {"processingStatus": "processing"},
        "contentDetails": {"duration": "PT1S"},
    })];
    let media = setup
        .root
        .path()
        .join("channels/talks/clips/aaaaaaaaaaa-01/video.mp4");
    let server = Server::start(
        &setup.root,
        Some(&setup.providers),
        &[("FAKE_MEDIA", setup.raw.to_str().unwrap())],
    );
    let base = server.base.as_str();
    setup.runtime.block_on(async {
        let refused = data(
            &call(
                base,
                "talks",
                "repair_upload",
                json!({"video_id": "broken-upload"}),
            )
            .await,
        );
        assert_eq!(wait_job(base, "talks", &refused).await["status"], "failed");
        {
            let mut youtube = setup.youtube.lock().unwrap();
            assert_eq!(youtube.sessions, 0);
            assert!(youtube.deleted.is_empty() && media.exists());
            youtube.videos[0]["status"]["privacyStatus"] = "private".into();
        }
        std::fs::remove_file(&media).unwrap();
        let job = data(
            &call(
                base,
                "talks",
                "repair_upload",
                json!({"video_id": "broken-upload"}),
            )
            .await,
        );
        let job = wait_job(base, "talks", &job).await;
        assert_eq!(job["status"], "succeeded", "{job}\n{}", server.log());
        {
            let youtube = setup.youtube.lock().unwrap();
            assert_eq!(youtube.sessions, 1);
            assert_eq!(youtube.deleted, ["broken-upload"]);
            assert_eq!(
                youtube.inserted["status"],
                json!({"privacyStatus": "private"})
            );
            let scheduled: jiff::Timestamp = youtube.videos[0]["status"]["publishAt"]
                .as_str()
                .unwrap()
                .parse()
                .unwrap();
            assert_eq!(scheduled, publish_at.parse::<jiff::Timestamp>().unwrap());
        }
        assert_eq!(row(&setup.root).youtube_id.as_deref(), Some("new-upload"));
        let resumed = data(
            &call(
                base,
                "talks",
                "repair_upload",
                json!({"video_id": "broken-upload"}),
            )
            .await,
        );
        assert_eq!(
            wait_job(base, "talks", &resumed).await["status"],
            "succeeded"
        );
        assert_eq!(setup.youtube.lock().unwrap().sessions, 1);
    });
}
