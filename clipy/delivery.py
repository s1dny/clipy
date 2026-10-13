"""Internal release proof without giving CI a Kubernetes credential."""

import os
import re
from pathlib import Path

import requests

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TARGETS = {"clipy", "clipy-site", "clipy-studio"}
REGISTRY = "git.aza.network/aza/clipy:"


def readiness() -> dict:
    revision = os.environ.get("CLIPY_REVISION", "")
    result = {"revision": revision, "ready": False}
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        return result
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    namespace = (SERVICE_ACCOUNT / "namespace").read_text().strip()
    root = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT_HTTPS']}"
    with requests.Session() as client:
        client.headers["Authorization"] = f"Bearer {token}"
        client.verify = str(SERVICE_ACCOUNT / "ca.crt")

        def read(path):
            response = client.get(root + path, timeout=3)
            response.raise_for_status()
            return response.json()["items"]

        deployments = read(f"/apis/apps/v1/namespaces/{namespace}/deployments")
        deployments = {
            d["metadata"]["name"]: d
            for d in deployments
            if d["metadata"]["name"] in TARGETS
        }
        if set(deployments) != TARGETS:
            return result
        images = []
        for deploy in deployments.values():
            status, desired = (
                deploy.get("status", {}),
                deploy["spec"].get("replicas", 1),
            )
            if not (
                status.get("observedGeneration", 0) >= deploy["metadata"]["generation"]
                and status.get("updatedReplicas", 0) == desired
                and status.get("readyReplicas", 0) >= desired
                and status.get("replicas", 0) == desired
            ):
                return result
            clipy = [
                c["image"]
                for c in deploy["spec"]["template"]["spec"]["containers"]
                if c["image"].startswith(REGISTRY)
            ]
            if not clipy:
                return result
            images.extend(clipy)
        jobs = read(f"/apis/batch/v1/namespaces/{namespace}/cronjobs")
        agent = next((j for j in jobs if j["metadata"]["name"] == "clipy-agent"), None)
        if not agent:
            return result
        images.extend(
            c["image"]
            for c in agent["spec"]["jobTemplate"]["spec"]["template"]["spec"][
                "containers"
            ]
        )
        prefix = f"{REGISTRY}main-{revision[:12]}-"
        result["ready"] = len(set(images)) == 1 and all(
            i.startswith(prefix) and "@sha256:" in i for i in images
        )
        return result
