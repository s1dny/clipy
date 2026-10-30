//! /releasez reports ready only when every workload runs this exact commit,
//! read from the Kubernetes API with the pod's own identity.

mod support;

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use serde_json::json;
use support::*;

const REVISION: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[test]
fn release_waits_for_all_workers() {
    let runtime = runtime();
    let lagging = Arc::new(AtomicBool::new(true));
    let image = format!(
        "git.aza.network/aza/clipy:main-{}-1@sha256:{}",
        &REVISION[..12],
        "b".repeat(64)
    );
    let handler = {
        let lagging = lagging.clone();
        Arc::new(move |req: Req| {
            if req.host != "kubernetes.test" {
                return Resp::status(404, json!({}));
            }
            assert_eq!(req.header("authorization"), "Bearer fixture-workload-token");
            let containers = json!([{"image": image}]);
            if req.path == "/apis/apps/v1/namespaces/clipy/deployments" {
                let items: Vec<_> = ["clipy", "clipy-site", "clipy-studio"]
                    .iter()
                    .map(|name| {
                        let ready = if lagging.load(Ordering::SeqCst) && *name == "clipy-studio" { 0 } else { 1 };
                        json!({
                            "metadata": {"name": name, "generation": 2},
                            "spec": {"replicas": 1, "template": {"spec": {"containers": containers}}},
                            "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1, "readyReplicas": ready},
                        })
                    })
                    .collect();
                Resp::json(json!({"items": items}))
            } else if req.path == "/apis/batch/v1/namespaces/clipy/cronjobs" {
                Resp::json(json!({"items": [{"metadata": {"name": "clipy-agent"},
                    "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": containers}}}}}}]}))
            } else {
                Resp::status(404, json!({}))
            }
        })
    };
    let providers = providers(&runtime, handler);
    let root = Root::new(CONFIG);
    let account = root.path().join("service-account");
    std::fs::create_dir_all(&account).unwrap();
    std::fs::write(account.join("token"), "fixture-workload-token").unwrap();
    std::fs::write(account.join("namespace"), "clipy").unwrap();
    std::fs::copy(&providers.ca, account.join("ca.crt")).unwrap();
    let server = Server::start(
        &root,
        Some(&providers),
        &[
            ("CLIPY_REVISION", REVISION),
            ("KUBERNETES_SERVICE_HOST", "kubernetes.test"),
            ("KUBERNETES_SERVICE_PORT_HTTPS", "443"),
            ("CLIPY_SERVICE_ACCOUNT", account.to_str().unwrap()),
        ],
    );
    let releasez = format!("{}/releasez", server.base);
    assert_eq!(ureq_get(&releasez).unwrap().0, 503);
    lagging.store(false, Ordering::SeqCst);
    let (status, body) = ureq_get(&releasez).unwrap();
    assert_eq!(status, 200, "{body}\n{}", server.log());
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&body).unwrap(),
        json!({"revision": REVISION, "ready": true})
    );
}
