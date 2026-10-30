//! Release proof for CI without giving CI a Kubernetes credential: every
//! Clipy workload runs this exact commit and is ready.

use std::path::PathBuf;

use anyhow::{Context, Result};
use serde_json::{Value, json};

const TARGETS: [&str; 3] = ["clipy", "clipy-site", "clipy-studio"];
const REGISTRY: &str = "git.aza.network/aza/clipy:";

pub async fn readiness() -> Value {
    let revision = std::env::var("CLIPY_REVISION").unwrap_or_default();
    let ready = check(&revision).await.unwrap_or_else(|error| {
        tracing::warn!("Release readiness unavailable: {error:#}");
        false
    });
    json!({ "revision": revision, "ready": ready })
}

async fn check(revision: &str) -> Result<bool> {
    if revision.len() != 40
        || !revision
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Ok(false);
    }
    let account = PathBuf::from(
        std::env::var("CLIPY_SERVICE_ACCOUNT")
            .unwrap_or_else(|_| "/var/run/secrets/kubernetes.io/serviceaccount".into()),
    );
    let token = std::fs::read_to_string(account.join("token"))?
        .trim()
        .to_owned();
    let namespace = std::fs::read_to_string(account.join("namespace"))?
        .trim()
        .to_owned();
    let ca = reqwest::Certificate::from_pem(&std::fs::read(account.join("ca.crt"))?)?;
    let client = reqwest::Client::builder()
        .tls_certs_only([ca])
        .timeout(std::time::Duration::from_secs(3))
        .build()?;
    let root = format!(
        "https://{}:{}",
        std::env::var("KUBERNETES_SERVICE_HOST").context("not in a cluster")?,
        std::env::var("KUBERNETES_SERVICE_PORT_HTTPS").context("not in a cluster")?
    );
    let read = |path: String| {
        let request = client.get(format!("{root}{path}")).bearer_auth(&token);
        async move {
            let body: Value = request.send().await?.error_for_status()?.json().await?;
            Ok::<_, anyhow::Error>(body["items"].as_array().cloned().unwrap_or_default())
        }
    };
    let deployments = read(format!("/apis/apps/v1/namespaces/{namespace}/deployments")).await?;
    let mut images = Vec::new();
    for target in TARGETS {
        let Some(deploy) = deployments.iter().find(|d| d["metadata"]["name"] == target) else {
            return Ok(false);
        };
        let status = &deploy["status"];
        let desired = deploy["spec"]["replicas"].as_i64().unwrap_or(1);
        let count = |key: &str| status[key].as_i64().unwrap_or(0);
        if !(count("observedGeneration") >= deploy["metadata"]["generation"].as_i64().unwrap_or(0)
            && count("updatedReplicas") == desired
            && count("readyReplicas") >= desired
            && count("replicas") == desired)
        {
            return Ok(false);
        }
        let ours: Vec<String> = containers(&deploy["spec"]["template"])
            .filter(|image| image.starts_with(REGISTRY))
            .collect();
        if ours.is_empty() {
            return Ok(false);
        }
        images.extend(ours);
    }
    let cronjobs = read(format!("/apis/batch/v1/namespaces/{namespace}/cronjobs")).await?;
    let Some(agent) = cronjobs
        .iter()
        .find(|j| j["metadata"]["name"] == "clipy-agent")
    else {
        return Ok(false);
    };
    images.extend(containers(
        &agent["spec"]["jobTemplate"]["spec"]["template"],
    ));
    let prefix = format!("{REGISTRY}main-{}-", &revision[..12]);
    images.sort();
    images.dedup();
    Ok(images.len() == 1 && images[0].starts_with(&prefix) && images[0].contains("@sha256:"))
}

fn containers(template: &Value) -> impl Iterator<Item = String> + '_ {
    template["spec"]["containers"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|c| c["image"].as_str().map(str::to_owned))
}
