//! The daily report to the owner: one Matrix message covering every channel.

use anyhow::{Context, Result};
use serde_json::{Value, json};

/// Post a Markdown message to $MATRIX_ROOM; returns its event id.
pub async fn send(http: &reqwest::Client, text: &str) -> Result<String> {
    let homeserver = std::env::var("MATRIX_HOMESERVER")
        .unwrap_or_else(|_| "https://matrix.aza.network".into());
    let token = std::env::var("MATRIX_ACCESS_TOKEN").context("MATRIX_ACCESS_TOKEN is not set")?;
    let room = std::env::var("MATRIX_ROOM").context("MATRIX_ROOM is not set")?;
    let mut html = String::new();
    pulldown_cmark::html::push_html(&mut html, pulldown_cmark::Parser::new(text));
    let url = format!(
        "{homeserver}/_matrix/client/v3/rooms/{}/send/m.room.message/clipy-{}",
        percent_encoding::utf8_percent_encode(&room, percent_encoding::NON_ALPHANUMERIC),
        uuid::Uuid::new_v4().simple()
    );
    let response: Value = http
        .put(url)
        .bearer_auth(token)
        .json(&json!({
            "msgtype": "m.text",
            "body": text,
            "format": "org.matrix.custom.html",
            "formatted_body": html,
        }))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    Ok(response["event_id"].as_str().unwrap_or_default().to_owned())
}
