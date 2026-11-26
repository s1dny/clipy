//! The HTTP server: one MCP endpoint per channel at /mcp/<channel>, the
//! observer's read-only endpoint at /observer, the report's stats header and
//! a health check. Everything but /healthz needs the bearer token.

use std::sync::Arc;

use anyhow::{Result, bail};
use axum::Router;
use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use rmcp::transport::streamable_http_server::{
    StreamableHttpServerConfig, StreamableHttpService, session::local::LocalSessionManager,
};

use crate::app::App;
use crate::jobs::Jobs;
use crate::mcp;

fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0, |acc, (x, y)| acc | (x ^ y)) == 0
}

async fn gate(State(expected): State<Arc<String>>, request: Request, next: Next) -> Response {
    if request.uri().path() == "/healthz" {
        return next.run(request).await;
    }
    let supplied = request
        .headers()
        .get("authorization")
        .map(|v| v.as_bytes())
        .unwrap_or_default();
    if !same(supplied, expected.as_bytes()) {
        return (StatusCode::UNAUTHORIZED, "unauthorized").into_response();
    }
    next.run(request).await
}

fn config() -> StreamableHttpServerConfig {
    StreamableHttpServerConfig::default()
        .with_legacy_session_mode(false)
        .with_json_response(true)
        .disable_allowed_hosts()
}

pub async fn serve(app: Arc<App>) -> Result<()> {
    let token = std::env::var("CLIPY_MCP_TOKEN").unwrap_or_default();
    if token.len() < 32 {
        bail!("CLIPY_MCP_TOKEN must be set to at least 32 characters.");
    }
    let channels = app.config.names();
    for channel in &channels {
        // Fail fast on a broken config.toml.
        app.channel_config(channel)?;
        std::fs::create_dir_all(app.channel_dir(channel))?;
    }
    // The agent writes speaker headshots here.
    std::fs::create_dir_all(app.headshots_dir())?;
    let jobs = Jobs::start(app.clone())?;
    crate::observations::start_sampler(app.clone());

    let mut router = Router::new()
        .route("/healthz", get(|| async { "ok" }))
        .route(
            "/stats",
            get({
                let app = app.clone();
                move || async move {
                    let app = app.clone();
                    match tokio::task::spawn_blocking(move || crate::stats::report(&app)).await {
                        Ok(Ok(text)) => (StatusCode::OK, text),
                        Ok(Err(error)) => (StatusCode::INTERNAL_SERVER_ERROR, format!("{error:#}")),
                        Err(error) => (StatusCode::INTERNAL_SERVER_ERROR, error.to_string()),
                    }
                }
            }),
        );
    let observer = {
        let app = app.clone();
        StreamableHttpService::new(
            move || Ok(mcp::Observer::new(app.clone())),
            LocalSessionManager::default().into(),
            config(),
        )
    };
    router = router.nest_service("/observer", observer);
    for channel in channels {
        let (app, jobs, name) = (app.clone(), jobs.clone(), channel.clone());
        let service = StreamableHttpService::new(
            move || Ok(mcp::Channel::new(app.clone(), jobs.clone(), name.clone())),
            LocalSessionManager::default().into(),
            config(),
        );
        router = router.nest_service(&format!("/mcp/{channel}"), service);
    }
    let router = router
        .fallback(|| async { (StatusCode::NOT_FOUND, "no such channel") })
        .layer(axum::middleware::from_fn_with_state(
            Arc::new(format!("Bearer {token}")),
            gate,
        ));
    let address = format!(
        "{}:{}",
        std::env::var("HOST").unwrap_or_else(|_| "0.0.0.0".into()),
        std::env::var("PORT").unwrap_or_else(|_| "8000".into())
    );
    let listener = tokio::net::TcpListener::bind(&address).await?;
    tracing::info!("Serving on {address}");
    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown())
        .await?;
    Ok(())
}

pub async fn shutdown() {
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("signal handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = terminate.recv() => {}
    }
}

/// The public site: the channels' landing and privacy pages.
pub async fn site(dir: std::path::PathBuf, port: u16) -> Result<()> {
    let router = Router::new().fallback_service(tower_http::services::ServeDir::new(dir));
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port)).await?;
    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown())
        .await?;
    Ok(())
}
