//! Speaker-labelled transcription with AssemblyAI, and the sentence
//! transcript the agent reads.

use std::path::Path;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

const API: &str = "https://api.assemblyai.com/v2";
const MAX_WAIT: Duration = Duration::from_secs(3600);

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Sentence {
    pub speaker: String,
    pub text: String,
    /// Source milliseconds.
    pub start: i64,
    pub end: i64,
}

/// Upload audio and wait for its transcript with speaker labels.
pub async fn transcribe(http: &reqwest::Client, audio: &Path, speech_model: &str) -> Result<Value> {
    let key = std::env::var("ASSEMBLYAI_API_KEY").context("ASSEMBLYAI_API_KEY is not set")?;
    let file = tokio::fs::File::open(audio).await?;
    let upload: Value = http
        .post(format!("{API}/upload"))
        .header("authorization", &key)
        .body(reqwest::Body::wrap_stream(file_stream(file)))
        .timeout(Duration::from_secs(3600))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let audio_url = upload["upload_url"]
        .as_str()
        .context("AssemblyAI returned no upload_url")?;
    let submitted: Value = http
        .post(format!("{API}/transcript"))
        .header("authorization", &key)
        .json(&json!({
            "audio_url": audio_url,
            "speech_models": [speech_model],
            "speaker_labels": true,
        }))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let id = submitted["id"]
        .as_str()
        .context("AssemblyAI returned no transcript id")?;
    let started = Instant::now();
    loop {
        if started.elapsed() > MAX_WAIT {
            bail!("Transcription timed out after {}s", MAX_WAIT.as_secs());
        }
        let poll = http
            .get(format!("{API}/transcript/{id}"))
            .header("authorization", &key)
            .timeout(Duration::from_secs(30))
            .send()
            .await
            .and_then(reqwest::Response::error_for_status);
        let data: Value = match poll {
            Ok(response) => response.json().await?,
            Err(error) => {
                tracing::warn!("Transcript poll failed ({error}); retrying");
                tokio::time::sleep(Duration::from_secs(5)).await;
                continue;
            }
        };
        match data["status"].as_str() {
            Some("completed") => return Ok(data),
            Some("error") => bail!(
                "Transcription failed: {}",
                data["error"].as_str().unwrap_or("unknown error")
            ),
            _ => tokio::time::sleep(Duration::from_secs(3)).await,
        }
    }
}

/// A file as a stream of chunks, for a streaming upload body.
pub fn file_stream(file: tokio::fs::File) -> impl futures::Stream<Item = std::io::Result<Vec<u8>>> {
    use tokio::io::AsyncReadExt;
    futures::stream::unfold(file, |mut file| async move {
        let mut buffer = vec![0; 1 << 20];
        match file.read(&mut buffer).await {
            Ok(0) => None,
            Ok(n) => {
                buffer.truncate(n);
                Some((Ok(buffer), file))
            }
            Err(error) => Some((Err(error), file)),
        }
    })
}

/// Split utterances into per-sentence segments using word-level timings.
pub fn sentences(transcript: &Value) -> Vec<Sentence> {
    let mut out = Vec::new();
    for u in transcript["utterances"].as_array().into_iter().flatten() {
        let speaker = u["speaker"].as_str().unwrap_or_default().to_owned();
        let words = u["words"].as_array().cloned().unwrap_or_default();
        if words.is_empty() {
            out.push(Sentence {
                speaker,
                text: u["text"].as_str().unwrap_or_default().to_owned(),
                start: u["start"].as_i64().unwrap_or(0),
                end: u["end"].as_i64().unwrap_or(0),
            });
            continue;
        }
        let mut buffer: Vec<&Value> = Vec::new();
        let flush = |buffer: &mut Vec<&Value>, out: &mut Vec<Sentence>| {
            if buffer.is_empty() {
                return;
            }
            out.push(Sentence {
                speaker: speaker.clone(),
                text: buffer
                    .iter()
                    .map(|w| w["text"].as_str().unwrap_or_default())
                    .collect::<Vec<_>>()
                    .join(" "),
                start: buffer[0]["start"].as_i64().unwrap_or(0),
                end: buffer[buffer.len() - 1]["end"].as_i64().unwrap_or(0),
            });
            buffer.clear();
        };
        for word in &words {
            buffer.push(word);
            let text = word["text"].as_str().unwrap_or_default().trim_end();
            if text.ends_with(['.', '?', '!']) {
                flush(&mut buffer, &mut out);
            }
        }
        flush(&mut buffer, &mut out);
    }
    out
}
