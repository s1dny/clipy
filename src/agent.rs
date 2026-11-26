//! The daily unattended runs: for each channel, one Claude Code session
//! driving that channel's MCP endpoint; then an observer session that reads
//! them all and writes the owner's actions, sent over Matrix as one message
//! under the stats header from the server. Sessions run one after another,
//! so they never compete for the subscription's limits; one failing does
//! not stop the rest.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

struct Settings {
    server: String,
    token: String,
    work: PathBuf,
    prompts: PathBuf,
    config: crate::config::Config,
    model: String,
    effort: Option<String>,
    retry: Duration,
    note: Option<String>,
}

impl Settings {
    fn from_env(home: &Path) -> Result<Self> {
        let env = |key: &str| std::env::var(key).ok().filter(|v| !v.is_empty());
        if env("CLAUDE_CODE_OAUTH_TOKEN").is_none() {
            bail!(
                "No Claude login: set CLAUDE_CODE_OAUTH_TOKEN to a token from `claude setup-token`."
            );
        }
        Ok(Self {
            server: env("CLIPY_SERVER").unwrap_or_else(|| "http://127.0.0.1:8000".into()),
            token: env("CLIPY_MCP_TOKEN").context("CLIPY_MCP_TOKEN is not set")?,
            work: env("CLIPY_AGENT_WORK_DIR")
                .map_or_else(|| PathBuf::from("/tmp/agent"), PathBuf::from),
            prompts: home.join("agent"),
            config: crate::config::Config::load(
                &env("CLIPY_CONFIG").map_or_else(|| home.join("config.toml"), PathBuf::from),
            )?,
            model: env("CLAUDE_MODEL").unwrap_or_else(|| "opus".into()),
            effort: env("CLAUDE_EFFORT"),
            retry: Duration::from_secs(
                env("CLIPY_QUOTA_RETRY_SECONDS")
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(900),
            ),
            note: env("CLIPY_AGENT_NOTE"),
        })
    }
}

/// What one Claude Code session did.
#[derive(Default)]
struct Run {
    log: Vec<String>,
    used_tools: bool,
    clipy_calls: usize,
    result: Option<Value>,
    seconds: u64,
}

impl Run {
    fn succeeded(&self) -> bool {
        self.clipy_calls > 0 && self.result.as_ref().is_some_and(|r| r["is_error"] != true)
    }

    /// Only a usage limit hit before any tool ran is retried; a retry must
    /// never repeat side effects.
    fn retryable(&self) -> bool {
        if self.used_tools {
            return false;
        }
        let Some(result) = &self.result else {
            return false;
        };
        let text = format!(
            "{} {}",
            result["result"].as_str().unwrap_or_default(),
            result["subtype"]
        )
        .to_lowercase();
        result["is_error"] == true
            && (result["api_error_status"] == 429
                || text.contains("usage limit")
                || text.contains("hit your limit"))
    }

    fn final_message(&self) -> String {
        match &self.result {
            Some(result) if result["is_error"] != true => {
                result["result"].as_str().unwrap_or_default().to_owned()
            }
            Some(result) => format!(
                "Run failed: {}",
                result["result"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or(result["subtype"].as_str().unwrap_or("error"))
            ),
            None => "Run failed: Claude produced no final message.".into(),
        }
    }

    fn footer(&self, with_calls: bool) -> String {
        let usage = self
            .result
            .as_ref()
            .map(|r| r["usage"].clone())
            .unwrap_or_default();
        let count = |key: &str| usage[key].as_i64().unwrap_or(0);
        let input = count("input_tokens")
            + count("cache_creation_input_tokens")
            + count("cache_read_input_tokens");
        let (minutes, seconds) = (self.seconds / 60, self.seconds % 60);
        let duration = if minutes > 0 {
            format!("{minutes}m {seconds:02}s")
        } else {
            format!("{seconds}s")
        };
        let mut footer = format!(
            "Run: {duration} · {} input tokens · {} output tokens",
            crate::app::thousands(input),
            crate::app::thousands(count("output_tokens"))
        );
        if with_calls {
            let n = self.clipy_calls;
            footer += &format!(" · {n} tool call{}", if n == 1 { "" } else { "s" });
        }
        footer
    }
}

/// One headless Claude Code session with a single MCP server named clipy.
/// Its console lines carry `name`, since channels run side by side.
async fn session(
    settings: &Settings,
    name: &str,
    work: &Path,
    endpoint: &str,
    prompt: &str,
) -> Result<Run> {
    tokio::fs::create_dir_all(work).await?;
    let config = work.join("mcp.json");
    let servers = json!({"mcpServers": {"clipy": {
        "type": "http",
        "url": format!("{}{endpoint}", settings.server),
        "headers": {"Authorization": format!("Bearer {}", settings.token)},
    }}});
    crate::app::write_atomic(&config, servers.to_string().as_bytes(), Some(0o600))?;
    let mut command = tokio::process::Command::new(
        std::env::var("CLAUDE_BIN").unwrap_or_else(|_| "claude".into()),
    );
    command
        .args([
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
        ])
        .args([
            "--dangerously-skip-permissions",
            "--strict-mcp-config",
            "--mcp-config",
        ])
        .arg(&config)
        .args(["--model", &settings.model]);
    if let Some(effort) = &settings.effort {
        command.args(["--effort", effort]);
    }
    let mut child = command
        .current_dir(work)
        .env("MCP_TOOL_TIMEOUT", "900000")
        // wait_for_job is silent for up to 14 minutes while it waits server-side.
        .env("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", "900000")
        .env("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true)
        .spawn()
        .context("could not start claude")?;
    let mut stdin = child.stdin.take().context("stdin")?;
    stdin.write_all(prompt.as_bytes()).await?;
    drop(stdin);
    let mut events = tokio::fs::File::create(work.join("events.jsonl")).await?;
    let mut lines = BufReader::new(child.stdout.take().context("stdout")?).lines();
    let mut run = Run::default();
    let mut tools: HashMap<String, String> = HashMap::new();
    let say = |run: &mut Run, line: String| {
        println!("[{name}] {line}");
        run.log.push(line);
    };
    while let Some(line) = lines.next_line().await? {
        events.write_all(format!("{line}\n").as_bytes()).await?;
        let Ok(event) = serde_json::from_str::<Value>(&line) else {
            say(&mut run, line);
            continue;
        };
        let blocks = event["message"]["content"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        match event["type"].as_str() {
            Some("assistant") => {
                for block in blocks {
                    match block["type"].as_str() {
                        Some("text") => say(
                            &mut run,
                            format!("claude: {}", block["text"].as_str().unwrap_or_default()),
                        ),
                        Some("tool_use") => {
                            run.used_tools = true;
                            let name = block["name"].as_str().unwrap_or_default().to_owned();
                            tools.insert(block["id"].as_str().unwrap_or_default().to_owned(), name);
                        }
                        _ => {}
                    }
                }
            }
            Some("user") => {
                for block in blocks.iter().filter(|b| b["type"] == "tool_result") {
                    let name = tools
                        .get(block["tool_use_id"].as_str().unwrap_or_default())
                        .cloned()
                        .unwrap_or_default();
                    let Some(tool) = name.strip_prefix("mcp__clipy__") else {
                        continue;
                    };
                    let failed = block["is_error"] == true;
                    if !failed {
                        run.clipy_calls += 1;
                    }
                    let detail = if failed {
                        format!(" {}", content_text(&block["content"]))
                    } else {
                        String::new()
                    };
                    say(
                        &mut run,
                        format!(
                            "mcp: clipy/{tool} ({}){detail}",
                            if failed { "failed" } else { "completed" }
                        ),
                    );
                }
            }
            Some("result") => {
                if event["is_error"] == true {
                    say(
                        &mut run,
                        format!("error: {}", event["result"].as_str().unwrap_or_default()),
                    );
                }
                run.result = Some(event);
            }
            _ => {}
        }
    }
    let status = child.wait().await?;
    if !status.success() && run.result.is_none() {
        say(&mut run, format!("claude exited with {status}"));
    }
    Ok(run)
}

fn content_text(content: &Value) -> String {
    let text = match content {
        Value::String(s) => s.clone(),
        Value::Array(items) => items
            .iter()
            .filter_map(|i| i["text"].as_str())
            .collect::<Vec<_>>()
            .join(" "),
        other => other.to_string(),
    };
    crate::app::tail(&text, 300).to_owned()
}

async fn run_channel(settings: &Settings, channel: &str) -> Result<Run> {
    let mut prompt = std::fs::read_to_string(settings.prompts.join("prompt.md"))?;
    prompt += &settings
        .config
        .channel(channel, &Default::default())?
        .policy;
    if let Some(note) = &settings.note {
        prompt += &format!(
            "\nOne-off instruction from the owner for this run (it overrides the rules above where they conflict):\n{note}\n"
        );
    }
    let started = Instant::now();
    let mut attempt = 1;
    loop {
        let mut run = session(
            settings,
            channel,
            &settings.work.join(channel),
            &format!("/mcp/{channel}"),
            &prompt,
        )
        .await?;
        if attempt >= 3 || !run.retryable() {
            run.seconds = started.elapsed().as_secs();
            return Ok(run);
        }
        println!(
            "[{channel}] Claude usage limit before any tools ran; retrying in {} minutes (attempt {}/3).",
            settings.retry.as_secs() / 60,
            attempt + 1
        );
        tokio::time::sleep(settings.retry).await;
        attempt += 1;
    }
}

fn summary(channel: &str, run: &Run) -> String {
    let log: String = run.log.iter().map(|line| format!("    {line}\n")).collect();
    let mut footer = run.footer(true);
    if !run.succeeded() {
        footer += "\nThis run failed.";
    }
    format!(
        "### {channel}\n\nLog:\n{log}\nFinal message:\n{}\n\n{footer}\n",
        run.final_message()
    )
}

pub async fn run(home: &Path, channels: &[String]) -> Result<bool> {
    let settings = Settings::from_env(home)?;
    let http = crate::app::http_client(Duration::from_secs(300))?;
    // Channels are independent, so their agents run side by side; the
    // server still renders one source at a time.
    let runs = futures::future::join_all(channels.iter().map(|channel| {
        let settings = &settings;
        async move {
            let run = match run_channel(settings, channel).await {
                Ok(run) => run,
                Err(error) => Run {
                    log: vec![format!("error: {error:#}")],
                    ..Default::default()
                },
            };
            println!("[{channel}] {}", run.footer(true));
            (channel.clone(), run)
        }
    }))
    .await;

    // The server builds the stats header, since only it holds the analytics.
    let stats = async {
        let response = http
            .get(format!("{}/stats", settings.server))
            .bearer_auth(&settings.token)
            .send()
            .await?;
        Ok::<_, anyhow::Error>(response.error_for_status()?.text().await?)
    }
    .await
    .unwrap_or_else(|_| "(The stats header is unavailable: the server did not answer.)".into());
    let mut prompt = std::fs::read_to_string(settings.prompts.join("observer.md"))?;
    prompt += &format!("{stats}\n\nRuns:\n\n");
    for (channel, run) in &runs {
        prompt += &format!("{}\n", summary(channel, run));
    }
    let started = Instant::now();
    let mut observer = session(
        &settings,
        "observer",
        &settings.work.join("observer"),
        "/observer",
        &prompt,
    )
    .await
    .unwrap_or_else(|error| Run {
        log: vec![format!("error: {error:#}")],
        ..Default::default()
    });
    observer.seconds = started.elapsed().as_secs();
    let actions = match &observer.result {
        Some(result)
            if result["is_error"] != true && !observer.final_message().trim().is_empty() =>
        {
            observer.final_message()
        }
        _ => "Owner actions: could not be checked automatically (report generation failed).".into(),
    };
    let mut report = format!("{stats}\n\n---\n\n{actions}\n\n---\n");
    for (channel, run) in &runs {
        report += &format!("{channel}: {}  \n", run.footer(true));
    }
    report += &format!("observer: {}", observer.footer(false));
    println!("{report}");
    std::fs::create_dir_all(&settings.work)?;
    std::fs::write(settings.work.join("report.md"), &report)?;
    // A Matrix outage never fails the run.
    if std::env::var("MATRIX_ACCESS_TOKEN").is_ok_and(|t| !t.is_empty())
        && let Err(error) = crate::matrix::send(&http, &report).await
    {
        eprintln!("clipy: Matrix report failed: {error:#}");
    }
    Ok(runs.iter().all(|(_, run)| run.succeeded()))
}
