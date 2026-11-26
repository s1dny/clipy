//! The daily runner drives Claude Code per channel, retries only a usage
//! limit hit before any tool ran, gives each channel its policy from
//! config.toml, and sends one report over Matrix.

mod support;

use std::sync::{Arc, Mutex};

use serde_json::{Value, json};
use support::*;

/// Claude Code's stream-json output, scripted by FAKE_MODE and attempt.
const FAKE_CLAUDE: &str = r#"#!/bin/sh
cat > prompt
n=$(( $(cat attempts 2>/dev/null || echo 0) + 1 ))
echo "$n" > attempts
emit() { printf '%s\n' "$1"; }
limited='{"type":"result","subtype":"success","is_error":true,"api_error_status":429,"result":"You have hit your usage limit.","usage":{"input_tokens":0,"output_tokens":0}}'
if [ "$(basename "$PWD")" = observer ]; then
  emit "$limited"
  exit 1
fi
emit '{"type":"system","subtype":"init","model":"claude-opus-5-5"}'
if [ "$FAKE_MODE" = partial ]; then
  emit '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"mcp__clipy__write_notes","input":{}}]}}'
  emit '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"Saved.","is_error":false}]}}'
  emit '{"type":"assistant","message":{"content":[{"type":"text","text":"Notebook updated."}]}}'
  emit "$limited"
  exit 1
fi
if [ "$n" = 1 ]; then
  emit "$limited"
  exit 1
fi
emit '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"mcp__clipy__read_notes","input":{}}]}}'
emit '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"(empty)","is_error":false}]}}'
emit '{"type":"assistant","message":{"content":[{"type":"text","text":"Channel run completed."}]}}'
emit '{"type":"result","subtype":"success","is_error":false,"result":"Channel run completed.","usage":{"input_tokens":100,"cache_read_input_tokens":80,"cache_creation_input_tokens":20,"output_tokens":10}}'
"#;

fn daily_run(mode: &str, attempts: &str, succeeds: bool) {
    let runtime = runtime();
    let messages: Arc<Mutex<Vec<Value>>> = Default::default();
    let handler = {
        let messages = messages.clone();
        Arc::new(move |req: Req| {
            if req.path == "/stats" {
                assert_eq!(req.header("authorization"), format!("Bearer {TOKEN}"));
                return Resp::bytes("text/plain", b"Views: **42**".to_vec());
            }
            if req.method == "PUT"
                && req
                    .path
                    .starts_with("/_matrix/client/v3/rooms/%21test%3Alocal/send/m.room.message/")
            {
                messages.lock().unwrap().push(req.json());
                return Resp::json(json!({"event_id": "$test-report"}));
            }
            Resp::status(404, json!({}))
        })
    };
    let providers = providers(&runtime, handler);
    let root = Root::new(CONFIG);
    root.tool("claude", FAKE_CLAUDE);
    let work = root.path().join("work");
    let output = command(&root, None, &["agent", "talks"])
        .env("CLAUDE_CODE_OAUTH_TOKEN", "fixture")
        .env("CLIPY_SERVER", providers.url())
        .env("CLIPY_AGENT_WORK_DIR", &work)
        .env("CLIPY_QUOTA_RETRY_SECONDS", "0")
        .env("MATRIX_HOMESERVER", providers.url())
        .env("MATRIX_ACCESS_TOKEN", "fake")
        .env("MATRIX_ROOM", "!test:local")
        .env("FAKE_MODE", mode)
        .output()
        .unwrap();
    let log = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.status.success(), succeeds, "{log}");
    assert_eq!(
        std::fs::read_to_string(work.join("talks/attempts"))
            .unwrap()
            .trim(),
        attempts,
        "{log}"
    );
    let prompt = std::fs::read_to_string(work.join("talks/prompt")).unwrap();
    assert!(
        prompt.contains("Channel:\nGoal: watch hours. Speaker: always Example Speaker."),
        "{prompt}"
    );
    let messages = messages.lock().unwrap();
    assert_eq!(messages.len(), 1, "{log}");
    let body = messages[0]["body"].as_str().unwrap();
    assert!(body.starts_with("Views: **42**"), "{body}");
    assert!(
        body.contains("Owner actions: could not be checked automatically"),
        "{body}"
    );
    assert!(
        messages[0]["formatted_body"]
            .as_str()
            .unwrap()
            .contains("<strong>42</strong>")
    );
    if succeeds {
        assert!(
            body.contains("talks: Run: ")
                && body.contains("200 input tokens · 10 output tokens · 1 tool call"),
            "{body}"
        );
    } else {
        assert!(log.contains("mcp: clipy/write_notes (completed)"), "{log}");
    }
}

#[test]
fn usage_limit_before_tools_is_retried() {
    daily_run("recover", "2", true);
}

#[test]
fn usage_limit_after_tools_is_reported_not_retried() {
    daily_run("partial", "1", false);
}
