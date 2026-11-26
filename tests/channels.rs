//! Edit two independent channels through the real MCP HTTP endpoints.

mod support;

use std::sync::Arc;

use serde_json::json;
use support::*;

const GREEN: [u8; 3] = [30, 200, 30];

/// A 1600x900 photo: the speaker in red on the left, green on the right.
fn photo() -> Vec<u8> {
    let image = image::RgbImage::from_fn(1600, 900, |x, _| {
        image::Rgb(if x < 800 { HEADSHOT_RED } else { GREEN })
    });
    let mut png = Vec::new();
    image
        .write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png)
        .unwrap();
    png
}

#[test]
fn channels_keep_notes_clips_and_artwork_separate() {
    let runtime = runtime();
    let providers = providers(
        &runtime,
        Arc::new(|req| {
            openrouter(&req)
                .or_else(|| (req.host == "images.test").then(|| Resp::bytes("image/png", photo())))
                .unwrap_or_else(|| Resp::status(404, json!({})))
        }),
    );
    let root = Root::new(CONFIG);
    clip_row(&root, "talks", "aaaaaaaaaaa", 1, "uploaded", None);
    clip_row(
        &root,
        "talks",
        "aaaaaaaaaaa",
        2,
        "pending",
        Some("Example Speaker"),
    );
    clip_row(
        &root,
        "talks",
        "aaaaaaaaaaa",
        3,
        "pending",
        Some("Example Speaker"),
    );
    clip_row(
        &root,
        "science",
        "bbbbbbbbbbb",
        1,
        "pending",
        Some("Robert Sapolsky"),
    );
    let server = Server::start(&root, Some(&providers), &[]);
    let base = server.base.as_str();

    assert_eq!(
        http(&format!("{base}/mcp/talks"), "POST", None).unwrap().0,
        401
    );
    assert_eq!(
        http(&format!("{base}/stats"), "GET", Some("Bearer wrong"))
            .unwrap()
            .0,
        401
    );
    assert_eq!(
        http(
            &format!("{base}/mcp/nobody"),
            "POST",
            Some(&format!("Bearer {TOKEN}"))
        )
        .unwrap()
        .0,
        404
    );

    runtime.block_on(async {
        data(&call(base, "talks", "write_notes", json!({"content": "Compiler experiment"})).await);
        data(&call(base, "science", "write_notes", json!({"content": "Neuroscience experiment"})).await);
        assert!(!text(&call(base, "science", "read_notes", json!({})).await).contains("Compiler"));
        let notes = text(&call(base, "talks", "read_notes", json!({})).await);
        assert!(notes.contains("Compiler experiment") && !notes.contains("Neuroscience"));
        // append=false replaces the notebook.
        data(&call(base, "talks", "write_notes", json!({"content": "Condensed", "append": false})).await);
        assert_eq!(text(&call(base, "talks", "read_notes", json!({})).await), "Condensed");

        data(&call(base, "talks", "edit_clip", json!({"clip_id": "aaaaaaaaaaa:02", "title": "Programming", "thumbnail_prompt": "a binary"})).await);
        assert!(is_error(&call(base, "talks", "edit_clip", json!({"clip_id": "aaaaaaaaaaa:02", "title": "A \u{2014} B"})).await));
        data(&call(base, "talks", "discard_clip", json!({"clip_id": "aaaaaaaaaaa:03", "reason": "off topic"})).await);
        let queue = data(&call(base, "talks", "list_queue", json!({})).await);
        let listed: Vec<(String, String)> = queue
            .as_array()
            .unwrap()
            .iter()
            .map(|c| (c["id"].as_str().unwrap().to_owned(), c["title"].as_str().unwrap().to_owned()))
            .collect();
        assert_eq!(listed, [("aaaaaaaaaaa:02".to_owned(), "Programming".to_owned())]);
        let science = data(&call(base, "science", "list_queue", json!({})).await);
        assert_eq!(science[0]["id"], "bbbbbbbbbbb:01");
        assert_eq!(science.as_array().unwrap().len(), 1);

        // A fixed template on the left, the illustration centred on the right.
        let thumbnail = image(&call(base, "talks", "view_thumbnail", json!({"clip_id": "aaaaaaaaaaa:02"})).await);
        let template = image::open(format!("{ROOT}/tests/fixtures/template.jpg")).unwrap().to_rgb8();
        let template = image::imageops::resize(&template, 1920, 1080, image::imageops::FilterType::Lanczos3);
        assert!(near(thumbnail.get_pixel(300, 300), template.get_pixel(300, 300).0, 30));
        assert!(near(thumbnail.get_pixel(1274, 540), BLUE, 30));

        // Channels without a template need the speaker's headshot.
        assert!(is_error(&call(base, "science", "edit_clip", json!({"clip_id": "bbbbbbbbbbb:01", "thumbnail_prompt": "a neuron"})).await));
        assert!(is_error(&call(base, "science", "headshot", json!({"speaker": "Robert Sapolsky"})).await));
        let save = |crop: serde_json::Value| {
            json!({"speaker": "Robert Sapolsky", "image_url": "https://images.test/photo.png", "crop": crop})
        };
        // Too small a crop for a 480x720 portrait is refused.
        let small = save(json!({"left": 0.0, "top": 0.0, "width": 0.1, "height": 0.5}));
        assert!(is_error(&call(base, "science", "headshot", small).await));
        // The uncropped photo's centred portrait straddles both halves.
        let panel = image(&call(base, "science", "headshot", save(serde_json::Value::Null)).await);
        assert_eq!((panel.width(), panel.height()), (720, 1080));
        assert!(near(panel.get_pixel(100, 540), HEADSHOT_RED, 30) && near(panel.get_pixel(620, 540), GREEN, 30));
        // Framed on the speaker: the left half only.
        let crop = save(json!({"left": 0.0, "top": 0.0, "width": 0.5, "height": 1.0}));
        let panel = image(&call(base, "science", "headshot", crop).await);
        assert!(near(panel.get_pixel(620, 540), HEADSHOT_RED, 30));
        // Another channel sees the same shared headshot.
        let shared = image(&call(base, "talks", "headshot", json!({"speaker": "Robert Sapolsky"})).await);
        assert!(near(shared.get_pixel(620, 540), HEADSHOT_RED, 30));
        data(&call(base, "science", "edit_clip", json!({"clip_id": "bbbbbbbbbbb:01", "thumbnail_prompt": "a neuron"})).await);
        let thumbnail = image(&call(base, "science", "view_thumbnail", json!({"clip_id": "bbbbbbbbbbb:01"})).await);
        assert!(near(thumbnail.get_pixel(360, 540), HEADSHOT_RED, 30));
        assert!(near(thumbnail.get_pixel(1370, 540), BLUE, 30));
        assert_eq!(data(&call(base, "talks", "list_queue", json!({})).await)[0]["title"], "Programming");

        // Prompt overrides apply per channel and keep required placeholders.
        assert!(is_error(&call(base, "talks", "update_prompts", json!({"description_template": "{clip_description}"})).await));
        let prompts = data(&call(base, "talks", "update_prompts", json!({"editorial_guidance": "Only Compiler."})).await);
        assert_eq!(prompts["editorial_guidance"], "Only Compiler.");
        assert_eq!(data(&call(base, "science", "get_prompts", json!({})).await)["editorial_guidance"], "Clips of {speaker}.");

        // The observer reads every channel, and cannot write.
        let rows = data(&call(base, "", "query", json!({"sql": "SELECT channel, status FROM clips ORDER BY channel, number"})).await);
        assert_eq!(rows["rows"].as_array().unwrap().len(), 4);
        assert!(is_error(&call(base, "", "query", json!({"sql": "DELETE FROM clips"})).await));
    });
}
