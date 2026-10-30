//! End-to-end harness. The real `clipy` binary runs against local providers:
//! a proxy terminates TLS for Google, Studio, OpenRouter, AssemblyAI and
//! Kubernetes with a test CA, so the app uses its production URLs unchanged.
//! Media, SQLite, ffmpeg and HTTP clients all run for real.
#![allow(dead_code)]

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use rmcp::ServiceExt;
use rmcp::model::{CallToolRequestParams, CallToolResult};
use rmcp::transport::StreamableHttpClientTransport;
use rmcp::transport::streamable_http_client::StreamableHttpClientTransportConfig;
use serde_json::{Value, json};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf};

pub const TOKEN: &str = "ssssssssssssssssssssssssssssssssssssssss";
pub const ROOT: &str = env!("CARGO_MANIFEST_DIR");
pub const BIN: &str = env!("CARGO_BIN_EXE_clipy");

pub const CONFIG: &str = r#"
[models]
image = "test/image"
speech = "test"

[defaults]
description_template = "{clip_description}\n\nOriginal: {original_url}"
editorial_guidance = "Clips of {speaker}."
illustration_prompt_template = "Draw {prompt} on white."

[channels.blow]
handle = "@JonathanBlowClips"
speaker = "Jonathan Blow"
thumbnail_template = "blow.jpg"

[channels.science]
handle = "@ScienceClipsDaily"
"#;

const HOSTS: [&str; 11] = [
    "www.googleapis.com",
    "youtube.googleapis.com",
    "youtubeanalytics.googleapis.com",
    "youtubereporting.googleapis.com",
    "oauth2.googleapis.com",
    "studio.youtube.com",
    "openrouter.ai",
    "api.assemblyai.com",
    "i.ytimg.com",
    "kubernetes.test",
    "images.test",
];

pub fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .unwrap()
}

/// A request as a provider sees it.
pub struct Req {
    pub method: String,
    pub host: String,
    pub path: String,
    pub query: Vec<(String, String)>,
    pub headers: hyper::HeaderMap,
    pub body: Bytes,
}

impl Req {
    pub fn arg(&self, name: &str) -> Option<String> {
        self.query
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.clone())
    }

    pub fn header(&self, name: &str) -> String {
        self.headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_owned()
    }

    pub fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap_or_default()
    }
}

pub enum Resp {
    Reply {
        status: u16,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    },
    /// Drop the connection without answering.
    Reset,
}

impl Resp {
    pub fn json(value: Value) -> Self {
        Self::status(200, value)
    }

    pub fn status(status: u16, value: Value) -> Self {
        Self::Reply {
            status,
            headers: vec![("content-type".into(), "application/json".into())],
            body: value.to_string().into_bytes(),
        }
    }

    pub fn bytes(content_type: &str, body: Vec<u8>) -> Self {
        Self::Reply {
            status: 200,
            headers: vec![("content-type".into(), content_type.into())],
            body,
        }
    }

    pub fn with_header(mut self, name: &str, value: &str) -> Self {
        if let Self::Reply { headers, .. } = &mut self {
            headers.push((name.into(), value.into()));
        }
        self
    }
}

pub type Handler = Arc<dyn Fn(Req) -> Resp + Send + Sync>;

/// The local providers: an HTTP proxy that intercepts TLS for known hosts,
/// and also answers plain HTTP.
pub struct Providers {
    pub port: u16,
    pub ca: PathBuf,
    _dir: tempfile::TempDir,
}

impl Providers {
    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }
}

struct Rewind<S> {
    prefix: Vec<u8>,
    inner: S,
}

impl<S: AsyncRead + Unpin> AsyncRead for Rewind<S> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        if !self.prefix.is_empty() {
            let n = self.prefix.len().min(buf.remaining());
            let bytes: Vec<u8> = self.prefix.drain(..n).collect();
            buf.put_slice(&bytes);
            return Poll::Ready(Ok(()));
        }
        Pin::new(&mut self.inner).poll_read(cx, buf)
    }
}

impl<S: AsyncWrite + Unpin> AsyncWrite for Rewind<S> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.inner).poll_write(cx, buf)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

async fn serve_http<S>(io: S, handler: Handler, default_host: Option<String>)
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let service =
        hyper::service::service_fn(move |request: hyper::Request<hyper::body::Incoming>| {
            let handler = handler.clone();
            let default_host = default_host.clone();
            async move {
                let (parts, body) = request.into_parts();
                let body = body
                    .collect()
                    .await
                    .map(|b| b.to_bytes())
                    .unwrap_or_default();
                let host = parts
                    .uri
                    .host()
                    .map(str::to_owned)
                    .or(default_host)
                    .or_else(|| {
                        parts
                            .headers
                            .get("host")
                            .and_then(|h| h.to_str().ok())
                            .map(|h| h.split(':').next().unwrap_or("").to_owned())
                    })
                    .unwrap_or_default();
                let query = parts
                    .uri
                    .query()
                    .map(|q| {
                        url::form_urlencoded::parse(q.as_bytes())
                            .into_owned()
                            .collect()
                    })
                    .unwrap_or_default();
                let request = Req {
                    method: parts.method.to_string(),
                    host,
                    path: parts.uri.path().to_owned(),
                    query,
                    headers: parts.headers,
                    body,
                };
                let response = tokio::task::spawn_blocking(move || handler(request))
                    .await
                    .unwrap();
                match response {
                    Resp::Reset => Err("reset"),
                    Resp::Reply {
                        status,
                        headers,
                        body,
                    } => {
                        let mut builder = hyper::Response::builder().status(status);
                        for (name, value) in headers {
                            builder = builder.header(name, value);
                        }
                        Ok(builder.body(Full::new(Bytes::from(body))).unwrap())
                    }
                }
            }
        });
    let _ = hyper::server::conn::http1::Builder::new()
        .keep_alive(true)
        .serve_connection(hyper_util::rt::TokioIo::new(io), service)
        .await;
}

/// Start the providers in the background of `runtime`.
pub fn providers(runtime: &tokio::runtime::Runtime, handler: Handler) -> Providers {
    let dir = tempfile::tempdir().unwrap();
    let key = rcgen::KeyPair::generate().unwrap();
    let mut params = rcgen::CertificateParams::new(Vec::<String>::new()).unwrap();
    params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    params
        .distinguished_name
        .push(rcgen::DnType::CommonName, "Clipy test CA");
    let ca = rcgen::CertifiedIssuer::self_signed(params, key).unwrap();
    let leaf_key = rcgen::KeyPair::generate().unwrap();
    let mut names: Vec<String> = HOSTS.iter().map(|h| h.to_string()).collect();
    names.push("127.0.0.1".into());
    let leaf = rcgen::CertificateParams::new(names)
        .unwrap()
        .signed_by(&leaf_key, &ca)
        .unwrap();
    let ca_path = dir.path().join("ca.pem");
    std::fs::write(&ca_path, ca.pem()).unwrap();
    let provider = Arc::new(rustls::crypto::aws_lc_rs::default_provider());
    let tls = rustls::ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(
            vec![leaf.der().clone(), ca.der().clone()],
            rustls::pki_types::PrivateKeyDer::Pkcs8(leaf_key.serialize_der().into()),
        )
        .unwrap();
    let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(tls));
    let listener = runtime
        .block_on(tokio::net::TcpListener::bind("127.0.0.1:0"))
        .unwrap();
    let port = listener.local_addr().unwrap().port();
    runtime.spawn(async move {
        loop {
            let Ok((mut socket, _)) = listener.accept().await else {
                continue;
            };
            let (handler, acceptor) = (handler.clone(), acceptor.clone());
            tokio::spawn(async move {
                let mut head = Vec::new();
                let mut byte = [0u8; 1];
                while !head.ends_with(b"\r\n\r\n") {
                    if socket.read(&mut byte).await.unwrap_or(0) == 0 {
                        return;
                    }
                    head.push(byte[0]);
                }
                let text = String::from_utf8_lossy(&head).to_string();
                if let Some(target) = text.strip_prefix("CONNECT ") {
                    let host = target.split(':').next().unwrap_or_default().to_owned();
                    if socket
                        .write_all(b"HTTP/1.1 200 Connection established\r\n\r\n")
                        .await
                        .is_err()
                    {
                        return;
                    }
                    if let Ok(stream) = acceptor.accept(socket).await {
                        serve_http(stream, handler, Some(host)).await;
                    }
                } else {
                    serve_http(
                        Rewind {
                            prefix: head,
                            inner: socket,
                        },
                        handler,
                        None,
                    )
                    .await;
                }
            });
        }
    });
    Providers {
        port,
        ca: ca_path,
        _dir: dir,
    }
}

/// A data directory with config.toml, and a bin directory for fake tools.
pub struct Root {
    pub dir: tempfile::TempDir,
}

impl Root {
    pub fn new(config: &str) -> Self {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("config.toml"), config).unwrap();
        std::fs::create_dir_all(dir.path().join("bin")).unwrap();
        Self { dir }
    }

    pub fn path(&self) -> &Path {
        self.dir.path()
    }

    pub fn db(&self) -> clipy::db::Db {
        clipy::db::Db::open(&self.path().join("clipy.db")).unwrap()
    }

    /// Put an executable script on the app's PATH.
    pub fn tool(&self, name: &str, script: &str) {
        let path = self.path().join("bin").join(name);
        std::fs::write(&path, script).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }

    pub fn env(&self, providers: Option<&Providers>) -> HashMap<String, String> {
        let mut env = HashMap::from([
            (
                "CLIPY_CONFIG".to_owned(),
                self.path().join("config.toml").display().to_string(),
            ),
            (
                "CLIPY_DATA_DIR".to_owned(),
                self.path().display().to_string(),
            ),
            ("CLIPY_MCP_TOKEN".to_owned(), TOKEN.to_owned()),
            ("OPENROUTER_API_KEY".to_owned(), "unused".to_owned()),
            ("ASSEMBLYAI_API_KEY".to_owned(), "unused".to_owned()),
            ("NO_PROXY".to_owned(), "127.0.0.1,localhost".to_owned()),
            (
                "PATH".to_owned(),
                format!(
                    "{}:{}",
                    self.path().join("bin").display(),
                    std::env::var("PATH").unwrap_or_default()
                ),
            ),
            ("RUST_LOG".to_owned(), "info".to_owned()),
        ]);
        if let Some(providers) = providers {
            env.insert("HTTPS_PROXY".into(), providers.url());
            env.insert("SSL_CERT_FILE".into(), providers.ca.display().to_string());
        }
        env
    }

    /// Write a valid YouTube token with every scope for a channel.
    pub fn token(&self, channel: &str) {
        let token = clipy::youtube::Token {
            client_id: "fixture".into(),
            client_secret: "fixture".into(),
            refresh_token: "unused".into(),
            scopes: clipy::youtube::SCOPES
                .iter()
                .map(|s| s.to_string())
                .collect(),
            access_token: Some("fixture-access".into()),
            expires_at: Some(jiff::Timestamp::now() + jiff::SignedDuration::from_hours(1)),
        };
        let path = self
            .path()
            .join("channels")
            .join(channel)
            .join("youtube-token.json");
        clipy::youtube::save_token(&path, &token).unwrap();
    }
}

pub fn command(root: &Root, providers: Option<&Providers>, args: &[&str]) -> Command {
    let mut command = Command::new(BIN);
    command
        .args(args)
        .current_dir(ROOT)
        .envs(root.env(providers));
    command
}

/// Run a clipy command to completion.
pub fn clipy(root: &Root, providers: Option<&Providers>, args: &[&str]) -> std::process::Output {
    command(root, providers, args).output().unwrap()
}

/// `clipy serve`, stopped when dropped.
pub struct Server {
    pub base: String,
    child: Child,
    pub log: PathBuf,
}

impl Server {
    pub fn start(root: &Root, providers: Option<&Providers>, extra: &[(&str, &str)]) -> Self {
        let port = free_port();
        let log = root.path().join(format!("server-{port}.log"));
        let output = std::fs::File::create(&log).unwrap();
        let child = command(root, providers, &["serve"])
            .env("HOST", "127.0.0.1")
            .env("PORT", port.to_string())
            .envs(extra.iter().map(|(k, v)| (k.to_string(), v.to_string())))
            .stdout(output.try_clone().unwrap())
            .stderr(output)
            .spawn()
            .unwrap();
        let mut server = Self {
            base: format!("http://127.0.0.1:{port}"),
            child,
            log,
        };
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            let ready = std::net::TcpStream::connect(("127.0.0.1", port)).is_ok()
                && ureq_get(&format!("{}/healthz", server.base))
                    .is_some_and(|(status, _)| status == 200);
            if ready {
                return server;
            }
            if let Ok(Some(status)) = server.child.try_wait() {
                panic!(
                    "server exited with {status}: {}",
                    std::fs::read_to_string(&server.log).unwrap()
                );
            }
            assert!(
                Instant::now() < deadline,
                "server did not start: {}",
                std::fs::read_to_string(&server.log).unwrap()
            );
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    pub fn log(&self) -> String {
        std::fs::read_to_string(&self.log).unwrap_or_default()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

/// A tiny blocking HTTP GET against the local server: (status, body).
pub fn ureq_get(url: &str) -> Option<(u16, String)> {
    http(url, "GET", None)
}

pub fn http(url: &str, method: &str, authorization: Option<&str>) -> Option<(u16, String)> {
    use std::io::{BufRead, BufReader, Read, Write};
    let rest = url.strip_prefix("http://")?;
    let (authority, path) = rest
        .split_once('/')
        .map_or((rest, "/".to_owned()), |(a, p)| (a, format!("/{p}")));
    let mut stream = std::net::TcpStream::connect(authority).ok()?;
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .ok()?;
    let auth = authorization
        .map(|a| format!("Authorization: {a}\r\n"))
        .unwrap_or_default();
    write!(stream, "{method} {path} HTTP/1.1\r\nHost: {authority}\r\n{auth}Content-Length: 0\r\nConnection: close\r\n\r\n").ok()?;
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).ok()?;
    let status = line.split_whitespace().nth(1)?.parse().ok()?;
    let (mut length, mut chunked) = (None, false);
    loop {
        line.clear();
        reader.read_line(&mut line).ok()?;
        let header = line.trim_end().to_lowercase();
        if header.is_empty() {
            break;
        }
        if let Some(value) = header.strip_prefix("content-length:") {
            length = value.trim().parse::<usize>().ok();
        }
        chunked |= header == "transfer-encoding: chunked";
    }
    let mut body = Vec::new();
    match length {
        Some(n) => {
            body.resize(n, 0);
            reader.read_exact(&mut body).ok()?;
        }
        None => {
            reader.read_to_end(&mut body).ok()?;
        }
    }
    let body = String::from_utf8_lossy(&body).into_owned();
    Some((status, if chunked { dechunk(&body) } else { body }))
}

fn dechunk(body: &str) -> String {
    let mut out = String::new();
    let mut rest = body;
    while let Some((size, tail)) = rest.split_once("\r\n") {
        let Ok(n) = usize::from_str_radix(size.trim(), 16) else {
            break;
        };
        if n == 0 {
            break;
        }
        out.push_str(&tail[..n]);
        rest = &tail[n + 2..];
    }
    out
}

/// Call one tool over a fresh MCP connection, as the agent does.
pub async fn call(base: &str, channel: &str, tool: &str, args: Value) -> CallToolResult {
    let endpoint = if channel.is_empty() {
        format!("{base}/observer")
    } else {
        format!("{base}/mcp/{channel}")
    };
    let transport = StreamableHttpClientTransport::from_config(
        StreamableHttpClientTransportConfig::with_uri(endpoint).auth_header(TOKEN),
    );
    let client = ().serve(transport).await.expect("MCP connection");
    let mut request = CallToolRequestParams::new(tool.to_owned());
    if let Value::Object(map) = args {
        request = request.with_arguments(map);
    }
    let result = match client.call_tool(request).await {
        Ok(result) => result,
        Err(error) => {
            CallToolResult::error(vec![rmcp::model::ContentBlock::text(error.to_string())])
        }
    };
    let _ = client.cancel().await;
    result
}

pub fn is_error(result: &CallToolResult) -> bool {
    result.is_error == Some(true)
}

pub fn text(result: &CallToolResult) -> String {
    result
        .content
        .iter()
        .filter_map(|block| block.as_text().map(|t| t.text.clone()))
        .collect::<Vec<_>>()
        .join("\n")
}

/// A successful tool result's JSON.
pub fn data(result: &CallToolResult) -> Value {
    assert!(!is_error(result), "tool failed: {}", text(result));
    let text = text(result);
    serde_json::from_str(&text).unwrap_or(Value::String(text))
}

pub fn image(result: &CallToolResult) -> image::RgbImage {
    use base64::Engine;
    assert!(!is_error(result), "tool failed: {}", text(result));
    let block = result
        .content
        .iter()
        .find_map(|b| b.as_image())
        .expect("an image");
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(&block.data)
        .unwrap();
    image::load_from_memory(&bytes).unwrap().to_rgb8()
}

pub fn near(pixel: &image::Rgb<u8>, color: [u8; 3], tolerance: i32) -> bool {
    pixel
        .0
        .iter()
        .zip(color)
        .all(|(a, b)| (*a as i32 - b as i32).abs() <= tolerance)
}

/// Follow a job through list_jobs until it finishes.
pub async fn wait_job(base: &str, channel: &str, job: &Value) -> Value {
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        let jobs = data(&call(base, channel, "list_jobs", json!({"limit": 100})).await);
        let found = jobs
            .as_array()
            .unwrap()
            .iter()
            .find(|j| j["id"] == job["id"])
            .cloned()
            .expect("job listed");
        if found["status"] == "succeeded" || found["status"] == "failed" {
            return found;
        }
        assert!(Instant::now() < deadline, "job did not finish: {found}");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

pub fn ffmpeg(args: &[&str]) -> Vec<u8> {
    let output = Command::new("ffmpeg")
        .args(["-v", "error", "-y"])
        .args(args)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "ffmpeg failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

pub fn ffprobe_json(path: &Path, extra: &[&str]) -> Value {
    let output = Command::new("ffprobe")
        .args(["-v", "error"])
        .args(extra)
        .args(["-of", "json"])
        .arg(path)
        .output()
        .unwrap();
    serde_json::from_slice(&output.stdout).unwrap()
}

/// One decoded RGB frame at a timestamp.
pub fn frame(path: &Path, at: f64) -> Vec<f64> {
    ffmpeg(&[
        "-ss",
        &at.to_string(),
        "-i",
        path.to_str().unwrap(),
        "-frames:v",
        "1",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ])
    .into_iter()
    .map(f64::from)
    .collect()
}

pub fn mean_abs_diff(a: &[f64], b: &[f64]) -> f64 {
    assert_eq!(a.len(), b.len());
    a.iter().zip(b).map(|(x, y)| (x - y).abs()).sum::<f64>() / a.len() as f64
}

/// A blue disc on white, as a PNG data URL: the fake image model's drawing.
pub fn disc_png() -> Vec<u8> {
    let mut image = image::RgbImage::from_pixel(1024, 1024, image::Rgb([255, 255, 255]));
    for (x, y, pixel) in image.enumerate_pixels_mut() {
        let (dx, dy) = (x as f64 - 512.0, y as f64 - 512.0);
        if dx * dx + dy * dy <= 312.0 * 312.0 {
            *pixel = image::Rgb(BLUE);
        }
    }
    let mut png = Vec::new();
    image
        .write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png)
        .unwrap();
    png
}

pub const BLUE: [u8; 3] = [40, 120, 220];
pub const HEADSHOT_RED: [u8; 3] = [200, 30, 30];

/// The image model always draws the disc.
pub fn openrouter(req: &Req) -> Option<Resp> {
    use base64::Engine;
    if req.host != "openrouter.ai" {
        return None;
    }
    let url = format!(
        "data:image/png;base64,{}",
        base64::engine::general_purpose::STANDARD.encode(disc_png())
    );
    Some(Resp::json(
        json!({"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]}),
    ))
}

/// Google's discovery service, from the vendored documents.
pub fn discovery(req: &Req) -> Option<Resp> {
    let rest = req.path.strip_prefix("/discovery/v1/apis/")?;
    let name = rest.trim_end_matches("/rest").replace('/', "_");
    let path = Path::new(ROOT)
        .join("tests/fixtures/discovery")
        .join(format!("{name}.json"));
    Some(match std::fs::read(path) {
        Ok(bytes) => Resp::bytes("application/json", bytes),
        Err(_) => Resp::status(404, json!({"error": "unknown api"})),
    })
}

/// A YouTube video resource for the fake Data API.
pub fn video(id: &str, privacy: &str, published: &str, extra: Value) -> Value {
    let mut video = json!({
        "id": id,
        "snippet": {"title": "Example", "categoryId": "28", "description": "Original: example",
                    "publishedAt": published, "channelId": "channel-owned"},
        "status": {"privacyStatus": privacy, "uploadStatus": "processed"},
        "contentDetails": {"duration": "PT10M"},
        "statistics": {"viewCount": "140", "likeCount": "7", "commentCount": "2"},
    });
    merge(&mut video, extra);
    video
}

pub fn merge(target: &mut Value, patch: Value) {
    match (target, patch) {
        (Value::Object(target), Value::Object(patch)) => {
            for (key, value) in patch {
                merge(target.entry(key).or_insert(Value::Null), value);
            }
        }
        (target, patch) => *target = patch,
    }
}

/// The YouTube Data API over a shared list of videos.
#[derive(Default)]
pub struct YouTube {
    pub videos: Vec<Value>,
    pub inserted: Value,
    pub sessions: usize,
    pub probes: usize,
    pub thumbnails: usize,
    pub reset: bool,
    pub deleted: Vec<String>,
    pub monetized: bool,
    pub subscribers: i64,
}

impl YouTube {
    pub fn handle(this: &Mutex<Self>, req: &Req) -> Option<Resp> {
        if req.host != "youtube.googleapis.com" {
            return None;
        }
        let mut state = this.lock().unwrap();
        let path = req.path.as_str();
        Some(match (req.method.as_str(), path) {
            ("GET", "/youtube/v3/channels") => Resp::json(json!({"items": [{
                "id": "channel-owned",
                "snippet": {"title": "Test Channel", "customUrl": "@jonathanblowclips"},
                "status": {"isChannelMonetizationEnabled": state.monetized},
                "statistics": {"subscriberCount": state.subscribers.to_string()},
                "contentDetails": {"relatedPlaylists": {"uploads": "uploads"}},
            }]})),
            // Like the real uploads playlist, an upload can appear twice.
            ("GET", "/youtube/v3/playlistItems") => Resp::json(json!({
                "items": state.videos.iter().chain(state.videos.first()).map(|v| json!({"contentDetails": {"videoId": v["id"]}})).collect::<Vec<_>>()
            })),
            ("GET", "/youtube/v3/videos") => {
                let ids = req.arg("id").unwrap_or_default();
                let ids: Vec<&str> = ids.split(',').collect();
                // One item per requested id, repeats included, as YouTube does.
                let items: Vec<&Value> = ids
                    .iter()
                    .filter_map(|id| state.videos.iter().find(|v| v["id"] == *id))
                    .collect();
                Resp::json(json!({ "items": items }))
            }
            ("PUT", "/youtube/v3/videos") => {
                let update = req.json();
                for video in state.videos.iter_mut().filter(|v| v["id"] == update["id"]) {
                    if let Some(status) = update.get("status") {
                        video["status"].as_object_mut().unwrap().remove("publishAt");
                        merge(&mut video["status"], status.clone());
                    }
                    if let Some(snippet) = update.get("snippet") {
                        merge(&mut video["snippet"], snippet.clone());
                    }
                }
                Resp::json(update)
            }
            ("DELETE", "/youtube/v3/videos") => {
                let id = req.arg("id").unwrap_or_default();
                state.deleted.push(id.clone());
                state.videos.retain(|v| v["id"] != id.as_str());
                Resp::Reply {
                    status: 204,
                    headers: vec![],
                    body: vec![],
                }
            }
            ("POST", "/upload/youtube/v3/videos") => {
                state.inserted = req.json();
                state.sessions += 1;
                Resp::json(json!({}))
                    .with_header("location", "https://youtube.googleapis.com/upload/session")
            }
            ("PUT", "/upload/session") => {
                if req.header("content-range").starts_with("bytes */") {
                    state.probes += 1;
                    return Some(Resp::status(308, json!({})));
                }
                if state.reset {
                    state.reset = false;
                    assert!(!req.body.is_empty());
                    return Some(Resp::Reset);
                }
                assert!(!req.body.is_empty());
                let inserted = state.inserted.clone();
                let mut status = inserted["status"].clone();
                status["uploadStatus"] = "processed".into();
                let mut snippet = inserted["snippet"].clone();
                snippet["publishedAt"] = jiff::Timestamp::now().to_string().into();
                state.videos.push(json!({
                    "id": "new-upload",
                    "snippet": snippet,
                    "status": status,
                    "contentDetails": {"duration": "PT1S"},
                    "processingDetails": {"processingStatus": "succeeded"},
                }));
                Resp::json(json!({"id": "new-upload"}))
            }
            ("POST", "/upload/youtube/v3/thumbnails/set") => {
                state.thumbnails += 1;
                Resp::json(json!({"items": []}))
            }
            _ => Resp::status(
                404,
                json!({"error": format!("no fake for {} {path}", req.method)}),
            ),
        })
    }
}

/// Insert a pending clip row with media files, as a render would leave it.
pub fn clip_row(
    root: &Root,
    channel: &str,
    source: &str,
    number: i64,
    status: &str,
    speaker: Option<&str>,
) -> clipy::queue::Clip {
    let clip = clipy::queue::Clip {
        channel: channel.into(),
        source_id: source.into(),
        number,
        status: status.into(),
        title: format!("Clip {number:02}"),
        description: "A clip.".into(),
        speaker: speaker.map(str::to_owned),
        tags: vec![],
        thumbnail_prompt: Some("a compiler".into()),
        chapters: vec![],
        score: None,
        start_ms: 0,
        end_ms: 600_000,
        duration_ms: 600_000,
        source: None,
        edit_report: None,
        created_at: jiff::Timestamp::now().to_string(),
        discard_reason: None,
        youtube_id: None,
        publish_at: None,
        uploaded_at: None,
        processing_status: None,
        thumbnail_pending: false,
        repair: None,
    };
    let dir = root
        .path()
        .join("channels")
        .join(channel)
        .join("clips")
        .join(format!("{source}-{number:02}"));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("video.mp4"), b"").unwrap();
    std::fs::copy(
        Path::new(ROOT).join("thumbnails/blow.jpg"),
        dir.join("thumbnail.jpg"),
    )
    .unwrap();
    let saved = clip.clone();
    root.db().with(move |c| saved.save(c)).unwrap();
    clip
}
