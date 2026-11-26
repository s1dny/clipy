//! sample_source over MCP: yt-dlp cuts an audio interval (which, like the
//! real one, needs ffmpeg), AssemblyAI transcribes it, and the agent gets
//! each speaker's share of the words.

mod support;

use std::sync::{Arc, Mutex};

use serde_json::{Value, json};
use support::*;

/// yt-dlp as the sampler uses it. -J prints the video's metadata; a
/// --download-sections download needs ffmpeg, from --ffmpeg-location (a
/// binary or its directory) or else from PATH, and fails as yt-dlp does
/// without one.
const YT_DLP: &str = r#"#!/bin/sh
location=""; sections=""; output=""; metadata=""
while [ $# -gt 0 ]; do
  case "$1" in
    -J) metadata=1 ;;
    --ffmpeg-location) location="$2"; shift ;;
    --download-sections) sections="$2"; shift ;;
    -o) output="$2"; shift ;;
    -f|--print) shift ;;
  esac
  shift
done
if [ -n "$metadata" ]; then
  echo '{"id": "eeeeeeeeeee", "title": "A long talk", "channel": "Example Channel", "duration": 3600}'
  exit 0
fi
if [ -n "$location" ]; then
  [ -d "$location" ] && location="$location/ffmpeg"
  [ -x "$location" ] && ffmpeg="$location"
else
  ffmpeg=$(command -v ffmpeg)
fi
if [ -z "$ffmpeg" ]; then
  echo "ERROR: You have requested downloading the video partially, but ffmpeg is not installed. Aborting due to --abort-on-error" >&2
  exit 1
fi
echo "$sections" >> "$(dirname "$0")/sections.log"
path=$(printf '%s' "$output" | sed 's/%(ext)s/wav/')
"$ffmpeg" -v error -y -f lavfi -i "sine=frequency=300:duration=2" "$path" || exit 1
echo "$path"
"#;

#[derive(Default)]
struct AssemblyAi {
    uploads: Vec<usize>,
    requests: Vec<Value>,
}

fn assemblyai(state: &Mutex<AssemblyAi>, req: &Req) -> Option<Resp> {
    if req.host != "api.assemblyai.com" {
        return None;
    }
    assert_eq!(req.header("authorization"), "unused");
    let mut state = state.lock().unwrap();
    Some(match (req.method.as_str(), req.path.as_str()) {
        ("POST", "/v2/upload") => {
            assert!(req.body.starts_with(b"RIFF"), "the sample is a WAV file");
            state.uploads.push(req.body.len());
            Resp::json(json!({"upload_url": "https://cdn.assemblyai.com/upload/sample"}))
        }
        ("POST", "/v2/transcript") => {
            state.requests.push(req.json());
            Resp::json(json!({"id": "sample-1", "status": "queued"}))
        }
        ("GET", "/v2/transcript/sample-1") => Resp::json(json!({
            "id": "sample-1",
            "status": "completed",
            "utterances": [
                {"speaker": "A", "text": "one two three four five six seven eight nine ten eleven twelve"},
                {"speaker": "B", "text": "one two three four"},
                {"speaker": "A", "text": "one two three four five six seven eight"},
            ],
        })),
        _ => Resp::status(404, json!({"error": "unknown"})),
    })
}

#[test]
fn sample_reports_each_speakers_share() {
    let runtime = runtime();
    let state = Arc::new(Mutex::new(AssemblyAi::default()));
    let providers = providers(&runtime, {
        let state = state.clone();
        Arc::new(move |req| {
            assemblyai(&state, &req).unwrap_or_else(|| Resp::status(404, json!({})))
        })
    });
    let root = Root::new(CONFIG);
    root.tool("yt-dlp", YT_DLP);
    let yt_dlp = root.path().join("bin/yt-dlp");

    // The fake fails like yt-dlp when ffmpeg cannot be found, so a bad
    // --ffmpeg-location (a bare name resolves against the working
    // directory) would fail this test.
    for location in ["ffmpeg", "/nonexistent/ffmpeg"] {
        let output = std::process::Command::new(&yt_dlp)
            .current_dir(root.path())
            .args(["--download-sections", "*0-1", "--ffmpeg-location", location])
            .args(["-o", root.path().join("x.%(ext)s").to_str().unwrap(), "url"])
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("ffmpeg is not installed"));
    }

    let server = Server::start(&root, Some(&providers), &[]);
    let base = server.base.as_str();
    let url = "https://www.youtube.com/watch?v=eeeeeeeeeee";
    runtime.block_on(async {
        for (start, end) in [(0.0, 901.0), (300.0, 200.0), (3500.0, 3700.0)] {
            let args = json!({"url": url, "start_seconds": start, "end_seconds": end});
            assert!(is_error(&call(base, "talks", "sample_source", args).await));
        }
        let args = json!({"url": url, "start_seconds": 600, "end_seconds": 900});
        let sample = call(base, "talks", "sample_source", args).await;
        let sample = data(&sample);
        assert_eq!(sample["title"], "A long talk", "{}", server.log());
        assert_eq!(sample["sampled_at_minutes"], json!([10.0]));
        assert_eq!(sample["sampled_minutes"], 5.0);
        assert_eq!(
            sample["speakers"],
            json!([
                {"speaker": "A", "share_of_words_percent": 83.0},
                {"speaker": "B", "share_of_words_percent": 17.0},
            ])
        );
        assert!(
            sample["transcript"]
                .as_str()
                .unwrap()
                .starts_with("A: one two three")
        );
    });
    let sections = std::fs::read_to_string(root.path().join("bin/sections.log")).unwrap();
    assert_eq!(sections.trim(), "*600-900");
    let state = state.lock().unwrap();
    assert_eq!(state.uploads.len(), 1);
    assert_eq!(state.requests[0]["speaker_labels"], true);
    assert_eq!(state.requests[0]["speech_models"], json!(["test"]));
}
