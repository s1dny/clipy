//! The clipy command.

use std::path::PathBuf;
use std::process::ExitCode;

use anyhow::{Result, bail};
use clap::{Parser, Subcommand};
use clipy::{agent, app, jobs, pipeline, server, studio, youtube};

#[derive(Parser)]
#[command(name = "clipy", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Serve the channels' MCP endpoints and run background jobs.
    Serve,
    /// Run the daily Claude sessions for these channels, then the report.
    Agent { channels: Vec<String> },
    /// Run the private browser's Studio request worker and the hourly collector.
    Studio,
    /// Collect fresh Studio analytics once.
    Collect { channel: Option<String> },
    /// Sign a channel in to YouTube.
    Auth { channel: String },
    /// Download and transcribe a source.
    Prepare { url: String },
    /// Print one page of a prepared transcript.
    Transcript {
        url: String,
        #[arg(long, default_value_t = 0)]
        offset: usize,
        #[arg(long, default_value_t = 200)]
        limit: usize,
    },
    /// Render clip selections (a JSON array file), or resume the saved ones.
    Process {
        channel: String,
        url: String,
        /// The source's main speaker; defaults to the channel's.
        #[arg(long)]
        speaker: Option<String>,
        #[arg(long)]
        clips: Option<PathBuf>,
    },
    /// Import the previous file-based data layout, once.
    Migrate,
    /// Serve the public site.
    Site {
        #[arg(long, default_value = "site")]
        dir: PathBuf,
        #[arg(long, default_value_t = 8080)]
        port: u16,
    },
}

#[tokio::main]
async fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,rmcp=warn".into()),
        )
        .with_writer(std::io::stderr)
        .init();
    match run(Cli::parse()).await {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("clipy: {error:#}");
            ExitCode::FAILURE
        }
    }
}

async fn run(cli: Cli) -> Result<bool> {
    if let Command::Site { dir, port } = cli.command {
        server::site(dir, port).await?;
        return Ok(true);
    }
    if let Command::Agent { channels } = &cli.command {
        return agent::run(&std::env::current_dir()?, channels).await;
    }
    let app = app::App::from_env()?;
    let known = |channel: &str| -> Result<()> {
        if !app.config()?.has(channel) {
            bail!("config.toml has no [channels.{channel}]");
        }
        Ok(())
    };
    let log = jobs::JobLog::console();
    match cli.command {
        Command::Serve => server::serve(app).await?,
        Command::Studio => {
            let worker = tokio::spawn(studio::serve_requests(app.clone()));
            let collector = tokio::spawn(async move {
                loop {
                    let ok = studio::collect_all(&app, None)
                        .await
                        .unwrap_or_else(|error| {
                            tracing::warn!("Studio collection failed: {error:#}");
                            false
                        });
                    tokio::time::sleep(std::time::Duration::from_secs(if ok { 3600 } else { 300 }))
                        .await;
                }
            });
            tokio::select! {
                result = worker => result??,
                _ = collector => {}
                _ = server::shutdown() => {}
            }
        }
        Command::Collect { channel } => {
            if let Some(channel) = &channel {
                known(channel)?;
            }
            return studio::collect_all(&app, channel.as_deref()).await;
        }
        Command::Auth { channel } => {
            known(&channel)?;
            let handle = app.channel_config(&channel)?.handle;
            youtube::authorize(&app, &channel, &handle).await?;
        }
        Command::Prepare { url } => {
            println!("{}", pipeline::prepare(&app, &url, false, &log).await?);
        }
        Command::Transcript { url, offset, limit } => {
            println!(
                "{}",
                pipeline::transcript_page(&app, &url, offset, limit).await?
            );
        }
        Command::Process {
            channel,
            url,
            speaker,
            clips,
        } => {
            known(&channel)?;
            let speaker = match speaker.or(app.channel_config(&channel)?.speaker) {
                Some(speaker) => speaker,
                None => bail!("this channel has no fixed speaker; pass --speaker"),
            };
            let selections = match clips {
                Some(path) => Some(serde_json::from_str(&std::fs::read_to_string(path)?)?),
                None => None,
            };
            pipeline::process(&app, &channel, &url, &speaker, selections, &log).await?;
        }
        Command::Migrate => clipy::migrate::run(&app)?,
        Command::Agent { .. } | Command::Site { .. } => unreachable!(),
    }
    Ok(true)
}
