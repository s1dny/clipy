//! Prepare, read and render sources through the real CLI, MCP, ffmpeg,
//! thumbnail API and queue.

mod support;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::{Value, json};
use support::*;

fn seed_transcript(root: &Root, source: &str, sentences: &Value) {
    let (source, sentences) = (source.to_owned(), sentences.to_string());
    root.db()
        .with(move |c| {
            c.execute(
                "INSERT INTO transcripts (video_id, sentences, created_at) VALUES (?1, ?2, '2026-10-30T00:00:00Z')",
                [source, sentences],
            )?;
            Ok(())
        })
        .unwrap();
}

fn queue(root: &Root, channel: &str) -> Vec<clipy::queue::Clip> {
    let channel = channel.to_owned();
    root.db()
        .with(move |c| clipy::queue::list(c, &channel, "all"))
        .unwrap()
}

fn samples(path: &Path, start: f64) -> Vec<f32> {
    ffmpeg(&[
        "-ss",
        &start.to_string(),
        "-i",
        path.to_str().unwrap(),
        "-t",
        "0.5",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "f32le",
        "-",
    ])
    .as_chunks::<4>().0.iter()
    .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    .collect()
}

fn correlation(a: &[f32], b: &[f32]) -> f64 {
    let n = a.len().min(b.len()) as f64;
    let mean = |x: &[f32]| x.iter().map(|v| *v as f64).sum::<f64>() / n;
    let (ma, mb) = (mean(a), mean(b));
    let (mut cov, mut va, mut vb) = (0.0, 0.0, 0.0);
    for (x, y) in a.iter().zip(b) {
        let (x, y) = (*x as f64 - ma, *y as f64 - mb);
        cov += x * y;
        va += x * x;
        vb += y * y;
    }
    cov / (va.sqrt() * vb.sqrt())
}

fn resume_exports_synchronized_media(codec: &str, extension: &str) {
    let runtime = runtime();
    let providers = providers(
        &runtime,
        Arc::new(|req| openrouter(&req).unwrap_or_else(|| Resp::status(404, json!({})))),
    );
    let root = Root::new(CONFIG);
    let source = root.path().join("sources/aaaaaaaaaaa");
    std::fs::create_dir_all(&source).unwrap();
    let reference = root.path().join(format!("reference.{extension}"));
    let encoding: &[&str] = if codec == "libx264" {
        &["-preset", "ultrafast"]
    } else {
        &["-deadline", "realtime", "-cpu-used", "8"]
    };
    let mut args = vec![
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=192x108:rate=30:duration=8",
        "-c:v",
        codec,
    ];
    args.extend(encoding);
    args.extend(["-threads", "1", "-g", "150", reference.to_str().unwrap()]);
    ffmpeg(&args);
    let audio = root.path().join("reference-audio.webm");
    ffmpeg(&[
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=sin(2*PI*(300*t+25*t*t)):s=48000:d=8",
        "-c:a",
        "libopus",
        audio.to_str().unwrap(),
    ]);
    std::fs::copy(&reference, source.join(format!("video.{extension}"))).unwrap();
    let sentences = json!([{"speaker": "A", "text": "A discussion.", "start": 0, "end": 8000}]);
    seed_transcript(&root, "aaaaaaaaaaa", &sentences);
    let url = "https://youtube.com/watch?v=aaaaaaaaaaa";
    let output = clipy(&root, Some(&providers), &["transcript", url]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap()["sentences"],
        sentences
    );
    let selections = root.path().join("selections.json");
    std::fs::write(
        &selections,
        json!([{
            "title": "Programming", "description": "A programming discussion.", "start_ms": 1300, "end_ms": 6300,
            "chapters": [], "speaker": "Jonathan Blow", "thumbnail_prompt": "a binary",
        }])
        .to_string(),
    )
    .unwrap();
    for attempt in 0..2 {
        // A restarted run still downloads audio; its finished render and queue are reused.
        std::fs::copy(&audio, source.join("audio.webm")).unwrap();
        let mut args = vec!["process", "blow", url];
        if attempt == 0 {
            args.extend(["--clips", selections.to_str().unwrap()]);
        }
        let output = clipy(&root, Some(&providers), &args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let clips = queue(&root, "blow");
    assert_eq!(clips.len(), 1);
    assert_eq!(clips[0].status, "pending");
    let app_dir = root.path().join("channels/blow/clips/aaaaaaaaaaa-01");
    let clip: PathBuf = app_dir.join("video.mp4");
    let thumbnail = image::open(app_dir.join("thumbnail.jpg")).unwrap();
    assert_eq!((thumbnail.width(), thumbnail.height()), (1920, 1080));
    let metadata = ffprobe_json(
        &clip,
        &[
            "-count_frames",
            "-show_entries",
            "stream=codec_type,codec_name,nb_read_frames:format=duration",
        ],
    );
    let stream = |kind: &str| {
        metadata["streams"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["codec_type"] == kind)
            .unwrap()
            .clone()
    };
    assert_eq!(stream("video")["codec_name"], "h264");
    assert_eq!(stream("audio")["codec_name"], "aac");
    let duration: f64 = metadata["format"]["duration"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!((duration - 5.0).abs() < 0.05, "duration {duration}");
    let frames: i64 = stream("video")["nb_read_frames"]
        .as_str()
        .unwrap()
        .parse()
        .unwrap();
    assert!((frames - 150).abs() <= 2, "frames {frames}");
    // Source media is reclaimed once every clip and thumbnail exists.
    assert!(!source.join(format!("video.{extension}")).exists());
    for at in [0.0, 4.0] {
        assert!(mean_abs_diff(&frame(&clip, at), &frame(&reference, 1.3 + at)) < 5.0);
        let (actual, expected) = (samples(&clip, at), samples(&audio, 1.3 + at));
        let length = actual.len().min(expected.len());
        // AAC/Opus decoding can shift a few samples. Match within 10ms, well
        // below a frame, so a wrong range or drifting audio still fails.
        let margin = 80usize;
        let best = (0..=2 * margin)
            .map(|shift| {
                correlation(
                    &actual[margin..length - margin],
                    &expected[shift..length - 2 * margin + shift],
                )
            })
            .fold(f64::MIN, f64::max);
        assert!(best > 0.97, "audio correlation {best}");
    }
}

#[test]
fn resume_exports_synchronized_h264() {
    resume_exports_synchronized_media("libx264", "mp4");
}

#[test]
fn resume_exports_synchronized_vp9() {
    resume_exports_synchronized_media("libvpx-vp9", "webm");
}

fn blue_source(source: &Path) {
    std::fs::create_dir_all(source).unwrap();
    ffmpeg(&[
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:size=320x180:rate=1/10:duration=640",
        "-c:v",
        "libx264",
        "-threads",
        "1",
        source.join("video.mp4").to_str().unwrap(),
    ]);
    ffmpeg(&[
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=8000:cl=mono:d=640",
        "-c:a",
        "pcm_s16le",
        source.join("audio.wav").to_str().unwrap(),
    ]);
}

#[test]
fn agent_reads_full_source_and_submits_renderable_selections() {
    let runtime = runtime();
    let providers = providers(
        &runtime,
        Arc::new(|req| openrouter(&req).unwrap_or_else(|| Resp::status(404, json!({})))),
    );
    let root = Root::new(CONFIG);
    let url = "https://youtube.com/watch?v=ccccccccccc";
    let utterances: Vec<Value> = ["A", "B", "A", "A"]
        .iter()
        .enumerate()
        .map(|(i, s)| json!({"speaker": s, "text": format!("Discussion {i}."), "start": i * 160000, "end": (i + 1) * 160000}))
        .collect();
    seed_transcript(&root, "ccccccccccc", &json!(utterances));
    let server = Server::start(&root, Some(&providers), &[]);
    let base = server.base.as_str();
    let source = root.path().join("sources/ccccccccccc");
    runtime.block_on(async {
        let prepared = wait_job(base, "blow", &data(&call(base, "blow", "prepare_source", json!({"url": url})).await)).await;
        assert_eq!(prepared["status"], "succeeded", "{prepared}");
        let mut sentences = Vec::new();
        let mut offset = Some(0);
        while let Some(at) = offset {
            let page = data(&call(base, "blow", "get_source_transcript", json!({"url": url, "offset": at, "limit": 1})).await);
            sentences.extend(page["sentences"].as_array().unwrap().clone());
            offset = page["next_offset"].as_u64();
        }
        assert_eq!(sentences, utterances);
        assert!(is_error(&call(base, "blow", "process_source", json!({"url": url})).await));
        assert!(is_error(&call(base, "blow", "get_source_transcript", json!({"url": "https://youtube.com/watch?v=../../x"})).await));
        assert!(is_error(&call(base, "blow", "process_source", json!({"url": url, "clips": [{"start_ms": 4, "end_ms": 2}]})).await));
        blue_source(&source);
        let selection = json!({
            "title": "An editorial choice",
            "description": "An accurate argument summary.",
            "start_ms": sentences[1]["start"],
            "end_ms": sentences[3]["end"],
            "thumbnail_prompt": "a bold object",
            "chapters": [],
            "source": {"name": "Fixture", "streamed_on": "2020-01-02", "evidence": "Fixture metadata"},
        });
        let with = |patch: Value| {
            let mut clip = selection.clone();
            merge(&mut clip, patch);
            clip
        };
        // The public contract rejects short cuts, missing dates and forbidden punctuation.
        for bad in [with(json!({"end_ms": 161000})), with(json!({"source": null})), with(json!({"description": "Forbidden \u{2014} punctuation."}))] {
            assert!(is_error(&call(base, "blow", "process_source", json!({"url": url, "clips": [bad]})).await));
        }
        let before = data(&call(base, "blow", "list_queue", json!({})).await);
        let beyond = data(&call(base, "blow", "process_source", json!({"url": url, "clips": [with(json!({"end_ms": 800000}))]})).await);
        assert_eq!(wait_job(base, "blow", &beyond).await["status"], "failed");
        let rendered = data(&call(base, "blow", "process_source", json!({"url": url, "clips": [selection.clone()]})).await);
        let rendered = wait_job(base, "blow", &rendered).await;
        assert_eq!(rendered["status"], "succeeded", "{rendered}");
        let listed = data(&call(base, "blow", "list_queue", json!({})).await);
        assert_eq!(listed.as_array().unwrap().len(), before.as_array().unwrap().len() + 1);
        assert_eq!(listed[0]["title"], selection["title"]);
        assert_eq!(listed[0]["description"], selection["description"]);
        let clip = root.path().join("channels/blow/clips/ccccccccccc-01/video.mp4");
        let duration: f64 = ffprobe_json(&clip, &["-show_entries", "format=duration"])["format"]["duration"].as_str().unwrap().parse().unwrap();
        assert!((duration - 480.0).abs() < 0.05);

        // A retry reuses the saved choices and export, without duplication.
        blue_source(&source);
        let resumed = wait_job(base, "blow", &data(&call(base, "blow", "process_source", json!({"url": url})).await)).await;
        assert_eq!(resumed["status"], "succeeded", "{resumed}");
        assert_eq!(data(&call(base, "blow", "list_queue", json!({})).await), listed);
        let changed = data(&call(base, "blow", "process_source", json!({"url": url, "clips": [with(json!({"start_ms": 0}))]})).await);
        assert_eq!(wait_job(base, "blow", &changed).await["status"], "failed");
        assert_eq!(data(&call(base, "blow", "list_queue", json!({})).await), listed);

        // Re-edit a pending export, preserving its full eight minutes.
        blue_source(&source);
        let attribution = json!({"name": "Fixture revised", "streamed_on": "2020-01-02", "evidence": "Fixture metadata"});
        let edit = data(&call(base, "blow", "edit_video", json!({"clip_id": "ccccccccccc:01", "source": attribution})).await);
        let edited = wait_job(base, "blow", &edit).await;
        assert_eq!(edited["status"], "succeeded", "{edited}");
        let report = data(&call(base, "blow", "get_edit_report", json!({"clip_id": "ccccccccccc:01"})).await);
        assert_eq!(report["report"]["duration_ms"], 480000);
        assert_eq!(report["report"]["source"]["name"], "Fixture revised");
        let frame = image(&call(base, "blow", "inspect_clip_frame", json!({"clip_id": "ccccccccccc:01", "at_ms": 1000})).await);
        assert_eq!((frame.width(), frame.height()), (320, 180));

        // A source can explicitly produce nothing, without media or a
        // headshot, and that decision survives a retry.
        seed_transcript(&root, "ddddddddddd", &json!(sentences));
        let empty = "https://youtube.com/watch?v=ddddddddddd";
        let science_before = data(&call(base, "science", "list_queue", json!({})).await);
        for args in [json!({"url": empty, "speaker": "Robert Sapolsky", "clips": []}), json!({"url": empty, "speaker": "Robert Sapolsky"})] {
            let job = data(&call(base, "science", "process_source", args).await);
            assert_eq!(wait_job(base, "science", &job).await["status"], "succeeded");
        }
        assert_eq!(data(&call(base, "science", "list_queue", json!({})).await), science_before);
        assert!(!root.path().join("channels/science/clips").exists());
    });
}

#[test]
fn cli_edited_ranges_context_hold_and_resume() {
    let runtime = runtime();
    let providers = providers(
        &runtime,
        Arc::new(|req| openrouter(&req).unwrap_or_else(|| Resp::status(404, json!({})))),
    );
    let root = Root::new(CONFIG);
    let source = root.path().join("sources/aaaaaaaaaaa");
    std::fs::create_dir_all(&source).unwrap();
    let video = root.path().join("reference.mp4");
    ffmpeg(&[
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=30:duration=6",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=400:duration=6",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "1",
        "-c:a",
        "aac",
        video.to_str().unwrap(),
    ]);
    std::fs::copy(&video, source.join("video.mp4")).unwrap();
    seed_transcript(
        &root,
        "aaaaaaaaaaa",
        &json!([{"start": 0, "end": 6000, "speaker": "A", "text": "A discussion."}]),
    );
    let plan = json!([{
        "title": "An edited discussion",
        "description": "An explanation.",
        "thumbnail_prompt": "a compiler",
        "start_ms": 500,
        "end_ms": 5500,
        "chapters": [],
        "keep": [
            {"start_ms": 500, "end_ms": 2000, "reason": "Opening argument"},
            {"start_ms": 3500, "end_ms": 5500, "reason": "Remove stream housekeeping"},
        ],
        "annotations": [{"start_ms": 0, "end_ms": 1000, "label": "Context", "text": "A compiler discussion.", "evidence": "Source transcript"}],
        "visual_holds": [{"start_ms": 2000, "end_ms": 2500, "source_ms": 1000, "reason": "Preserve the completed original drawing"}],
    }]);
    let selections = root.path().join("selections.json");
    std::fs::write(&selections, plan.to_string()).unwrap();
    let url = "https://youtube.com/watch?v=aaaaaaaaaaa";
    for attempt in 0..2 {
        std::fs::copy(&video, source.join("audio.mp4")).unwrap();
        let mut args = vec!["process", "blow", url];
        if attempt == 0 {
            args.extend(["--clips", selections.to_str().unwrap()]);
        }
        let output = clipy(&root, Some(&providers), &args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let clips = queue(&root, "blow");
    assert_eq!(clips.len(), 1);
    assert_eq!(clips[0].duration_ms, 3500);
    let report = clips[0].edit_report.clone().unwrap();
    assert_eq!(report["duration_ms"], 3500);
    assert!((report["verification"]["duration_seconds"].as_f64().unwrap() - 3.5).abs() < 0.06);
    assert_eq!(
        report["cuts"],
        json!([{"output_ms": 1500, "removed_source_start_ms": 2000, "removed_source_end_ms": 3500, "reason": "Remove stream housekeeping"}])
    );
    assert_eq!(report["annotations"][0]["evidence"], "Source transcript");
    let clip = root
        .path()
        .join("channels/blow/clips/aaaaaaaaaaa-01/video.mp4");
    // The content after the cut and the held frame, not just the duration.
    assert!(mean_abs_diff(&frame(&clip, 2.75), &frame(&video, 4.75)) < 8.0);
    assert!(mean_abs_diff(&frame(&clip, 2.2), &frame(&video, 1.0)) < 8.0);
    let (original, edited) = (frame(&video, 1.0), frame(&clip, 0.5));
    let row = 640 * 3;
    // The top half is untouched; the card covers the bottom left.
    assert!(mean_abs_diff(&original[..180 * row], &edited[..180 * row]) < 8.0);
    let region = |pixels: &[f64]| -> Vec<f64> {
        (315..340)
            .flat_map(|y| pixels[y * row + 20 * 3..y * row + 140 * 3].to_vec())
            .collect()
    };
    assert!(mean_abs_diff(&region(&original), &region(&edited)) > 15.0);
}
