//! A small Chrome DevTools Protocol client for the private signed-in browser:
//! open and close our own tab, navigate, evaluate, and read cookies.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use anyhow::{Context, Result, bail};
use futures::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::sync::{Mutex, oneshot};
use tokio_tungstenite::tungstenite::Message;

type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>;
type Sink = futures::stream::SplitSink<
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
    Message,
>;

pub struct Browser {
    sink: Mutex<Sink>,
    pending: Pending,
    next: AtomicU64,
    reader: tokio::task::JoinHandle<()>,
}

pub fn endpoint() -> String {
    std::env::var("CLIPY_STUDIO_CDP").unwrap_or_else(|_| "http://127.0.0.1:9222".into())
}

/// The browser's version metadata, including its real user agent.
pub async fn version(http: &reqwest::Client) -> Result<Value> {
    Ok(http
        .get(format!("{}/json/version", endpoint()))
        .timeout(Duration::from_secs(3))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?)
}

impl Browser {
    pub async fn connect(http: &reqwest::Client) -> Result<Self> {
        let info = version(http).await?;
        let url = info["webSocketDebuggerUrl"]
            .as_str()
            .context("no webSocketDebuggerUrl")?;
        let (socket, _) = tokio::time::timeout(
            Duration::from_secs(15),
            tokio_tungstenite::connect_async(url),
        )
        .await
        .context("browser connection timed out")??;
        let (sink, mut stream) = socket.split();
        let pending: Pending = Default::default();
        let replies = pending.clone();
        let reader = tokio::spawn(async move {
            while let Some(Ok(message)) = stream.next().await {
                let Message::Text(text) = message else {
                    continue;
                };
                let Ok(value) = serde_json::from_str::<Value>(&text) else {
                    continue;
                };
                if let Some(id) = value["id"].as_u64()
                    && let Some(reply) = replies.lock().await.remove(&id)
                {
                    let _ = reply.send(value);
                }
            }
        });
        Ok(Self {
            sink: Mutex::new(sink),
            pending,
            next: AtomicU64::new(1),
            reader,
        })
    }

    /// Send a command, optionally to an attached tab's session.
    pub async fn call(&self, session: Option<&str>, method: &str, params: Value) -> Result<Value> {
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        let (reply, receiver) = oneshot::channel();
        self.pending.lock().await.insert(id, reply);
        let mut message = json!({ "id": id, "method": method, "params": params });
        if let Some(session) = session {
            message["sessionId"] = session.into();
        }
        self.sink
            .lock()
            .await
            .send(Message::text(message.to_string()))
            .await?;
        let response = tokio::time::timeout(Duration::from_secs(90), receiver)
            .await
            .with_context(|| format!("{method} timed out"))?
            .context("browser connection closed")?;
        if let Some(error) = response.get("error") {
            bail!("{method} failed: {error}");
        }
        Ok(response["result"].clone())
    }

    /// Every cookie in the browser's jar.
    pub async fn cookies(&self) -> Result<Vec<Value>> {
        let result = self.call(None, "Storage.getCookies", json!({})).await?;
        Ok(result["cookies"].as_array().cloned().unwrap_or_default())
    }

    /// Open a tab of our own; it is closed again when `f` finishes.
    pub async fn with_tab<T>(&self, f: impl AsyncFnOnce(&Tab<'_>) -> Result<T>) -> Result<T> {
        let created = self
            .call(None, "Target.createTarget", json!({"url": "about:blank"}))
            .await?;
        let target = created["targetId"]
            .as_str()
            .context("no targetId")?
            .to_owned();
        let result = async {
            let attached = self
                .call(
                    None,
                    "Target.attachToTarget",
                    json!({"targetId": target, "flatten": true}),
                )
                .await?;
            let session = attached["sessionId"]
                .as_str()
                .context("no sessionId")?
                .to_owned();
            f(&Tab {
                browser: self,
                session,
            })
            .await
        }
        .await;
        let _ = self
            .call(None, "Target.closeTarget", json!({"targetId": target}))
            .await;
        result
    }
}

impl Drop for Browser {
    fn drop(&mut self) {
        self.reader.abort();
    }
}

pub struct Tab<'a> {
    browser: &'a Browser,
    session: String,
}

impl Tab<'_> {
    /// Evaluate an expression, awaiting a promise, and return its JSON value.
    pub async fn evaluate(&self, expression: &str) -> Result<Value> {
        let result = self
            .browser
            .call(
                Some(&self.session),
                "Runtime.evaluate",
                json!({"expression": expression, "awaitPromise": true, "returnByValue": true}),
            )
            .await?;
        if let Some(exception) = result.get("exceptionDetails") {
            let message = exception["exception"]["description"]
                .as_str()
                .or_else(|| exception["text"].as_str())
                .unwrap_or("script failed");
            bail!("{message}");
        }
        Ok(result["result"]["value"].clone())
    }

    /// Navigate and wait for the document to be parsed.
    pub async fn goto(&self, url: &str) -> Result<()> {
        self.browser
            .call(Some(&self.session), "Page.navigate", json!({"url": url}))
            .await?;
        for _ in 0..600 {
            tokio::time::sleep(Duration::from_millis(100)).await;
            let state = self
                .evaluate("document.readyState")
                .await
                .unwrap_or_default();
            let href = self.evaluate("location.href").await.unwrap_or_default();
            if href != "about:blank" && (state == "interactive" || state == "complete") {
                return Ok(());
            }
        }
        bail!("Navigation to {url} timed out")
    }
}
