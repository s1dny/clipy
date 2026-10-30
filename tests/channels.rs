//! Edit two independent channels through the real MCP HTTP endpoints.

mod support;

use std::sync::Arc;

use serde_json::json;
use support::*;

#[test]
fn channels_keep_notes_clips_and_artwork_separate() {
    let runtime = runtime();
    let providers = providers(
        &runtime,
        Arc::new(|req| openrouter(&req).unwrap_or_else(|| Resp::status(404, json!({})))),
    );
    let root = Root::new(CONFIG);
    clip_row(&root, "blow", "aaaaaaaaaaa", 1, "uploaded", None);
    clip_row(
        &root,
        "blow",
        "aaaaaaaaaaa",
        2,
        "pending",
        Some("Jonathan Blow"),
    );
    clip_row(
        &root,
        "blow",
        "aaaaaaaaaaa",
        3,
        "pending",
        Some("Jonathan Blow"),
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
        http(&format!("{base}/mcp/blow"), "POST", None).unwrap().0,
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
        data(&call(base, "blow", "write_notes", json!({"content": "Jai experiment"})).await);
        data(&call(base, "science", "write_notes", json!({"content": "Neuroscience experiment"})).await);
        assert!(!text(&call(base, "science", "read_notes", json!({})).await).contains("Jai"));
        let notes = text(&call(base, "blow", "read_notes", json!({})).await);
        assert!(notes.contains("Jai experiment") && !notes.contains("Neuroscience"));
        // append=false replaces the notebook.
        data(&call(base, "blow", "write_notes", json!({"content": "Condensed", "append": false})).await);
        assert_eq!(text(&call(base, "blow", "read_notes", json!({})).await), "Condensed");

        data(&call(base, "blow", "edit_clip", json!({"clip_id": "aaaaaaaaaaa:02", "title": "Programming", "thumbnail_prompt": "a binary"})).await);
        assert!(is_error(&call(base, "blow", "edit_clip", json!({"clip_id": "aaaaaaaaaaa:02", "title": "A \u{2014} B"})).await));
        data(&call(base, "blow", "discard_clip", json!({"clip_id": "aaaaaaaaaaa:03", "reason": "off topic"})).await);
        let queue = data(&call(base, "blow", "list_queue", json!({})).await);
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
        let thumbnail = image(&call(base, "blow", "view_thumbnail", json!({"clip_id": "aaaaaaaaaaa:02"})).await);
        let template = image::open(format!("{ROOT}/thumbnails/blow.jpg")).unwrap().to_rgb8();
        let template = image::imageops::resize(&template, 1920, 1080, image::imageops::FilterType::Lanczos3);
        assert!(near(thumbnail.get_pixel(300, 300), template.get_pixel(300, 300).0, 30));
        assert!(near(thumbnail.get_pixel(1274, 540), BLUE, 30));

        // Channels without a template need the speaker's headshot.
        assert!(is_error(&call(base, "science", "edit_clip", json!({"clip_id": "bbbbbbbbbbb:01", "thumbnail_prompt": "a neuron"})).await));
        let headshot = root.path().join("headshots/robert-sapolsky.jpg");
        std::fs::create_dir_all(headshot.parent().unwrap()).unwrap();
        image::RgbImage::from_pixel(1600, 900, image::Rgb(HEADSHOT_RED)).save(&headshot).unwrap();
        data(&call(base, "science", "edit_clip", json!({"clip_id": "bbbbbbbbbbb:01", "thumbnail_prompt": "a neuron"})).await);
        let thumbnail = image(&call(base, "science", "view_thumbnail", json!({"clip_id": "bbbbbbbbbbb:01"})).await);
        assert!(near(thumbnail.get_pixel(360, 540), HEADSHOT_RED, 30));
        assert!(near(thumbnail.get_pixel(1370, 540), BLUE, 30));
        assert_eq!(data(&call(base, "blow", "list_queue", json!({})).await)[0]["title"], "Programming");

        // Prompt overrides apply per channel and keep required placeholders.
        assert!(is_error(&call(base, "blow", "update_prompts", json!({"description_template": "{clip_description}"})).await));
        let prompts = data(&call(base, "blow", "update_prompts", json!({"editorial_guidance": "Only Jai."})).await);
        assert_eq!(prompts["editorial_guidance"], "Only Jai.");
        assert_eq!(data(&call(base, "science", "get_prompts", json!({})).await)["editorial_guidance"], "Clips of {speaker}.");

        // The observer reads every channel, and cannot write.
        let rows = data(&call(base, "", "query", json!({"sql": "SELECT channel, status FROM clips ORDER BY channel, number"})).await);
        assert_eq!(rows["rows"].as_array().unwrap().len(), 4);
        assert!(is_error(&call(base, "", "query", json!({"sql": "DELETE FROM clips"})).await));
    });
}
