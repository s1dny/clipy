"""Exercise the live release endpoint through MCP and Kubernetes HTTP clients."""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from test_mcp_server import CONFIG, TOKEN, _free_port

REVISION = "a" * 40
IMAGE = f"git.aza.network/aza/clipy:main-{REVISION[:12]}-1@sha256:" + "b" * 64


class Cluster(BaseHTTPRequestHandler):
    lagging = False

    def do_GET(self):
        assert self.headers["Authorization"] == "Bearer fixture-workload-token"
        if self.path.endswith("/deployments"):
            items = [
                {
                    "metadata": {"name": name, "generation": 2},
                    "spec": {
                        "replicas": 1,
                        "template": {"spec": {"containers": [{"image": IMAGE}]}},
                    },
                    "status": {
                        "observedGeneration": 2,
                        "replicas": 1,
                        "updatedReplicas": 1,
                        "readyReplicas": 0
                        if self.lagging and name == "clipy-studio"
                        else 1,
                    },
                }
                for name in ("clipy", "clipy-site", "clipy-studio")
            ]
        elif self.path.endswith("/cronjobs"):
            items = [
                {
                    "metadata": {"name": "clipy-agent"},
                    "spec": {
                        "jobTemplate": {
                            "spec": {
                                "template": {"spec": {"containers": [{"image": IMAGE}]}}
                            }
                        }
                    },
                }
            ]
        else:
            self.send_error(404)
            return
        raw = json.dumps({"items": items}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("lagging", [False, True])
def test_release_endpoint_proves_all_workers_without_exposing_credentials(
    tmp_path, lagging
):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    account = tmp_path / "service-account"
    account.mkdir()
    (account / "token").write_text("fixture-workload-token")
    (account / "namespace").write_text("clipy")
    (account / "ca.crt").write_text("fixture-ca")
    Cluster.lagging = lagging
    fake = ThreadingHTTPServer(("127.0.0.1", 0), Cluster)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    port = _free_port()
    launcher = """
import copy, os, requests
from pathlib import Path
from requests.adapters import HTTPAdapter
import clipy.delivery as delivery
delivery.SERVICE_ACCOUNT = Path(os.environ['FIXTURE_ACCOUNT'])
class Transport(HTTPAdapter):
    def send(self, request, **kwargs):
        from urllib.parse import urlsplit
        redirected = copy.copy(request)
        redirected.url = os.environ['FIXTURE_CLUSTER'] + urlsplit(request.url).path
        return super().send(redirected, **kwargs)
original_init = requests.Session.__init__
def init(self):
    original_init(self)
    self.mount('https://cluster.test:443/', Transport())
requests.Session.__init__ = init
from clipy.mcp_server import main
main()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", launcher],
        env={
            **os.environ,
            "CLIPY_CONFIG": str(config),
            "CLIPY_DATA_DIR": str(tmp_path),
            "CLIPY_MCP_TOKEN": TOKEN,
            "CLIPY_REVISION": REVISION,
            "KUBERNETES_SERVICE_HOST": "cluster.test",
            "KUBERNETES_SERVICE_PORT_HTTPS": "443",
            "FIXTURE_ACCOUNT": str(account),
            "FIXTURE_CLUSTER": f"http://127.0.0.1:{fake.server_port}",
            "HOST": "127.0.0.1",
            "PORT": str(port),
        },
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if requests.get(f"http://127.0.0.1:{port}/healthz", timeout=1).ok:
                    break
            except requests.ConnectionError:
                pass
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        result = requests.get(f"http://127.0.0.1:{port}/releasez", timeout=10)
        assert result.status_code == (503 if lagging else 200), result.text
        assert result.json() == {"revision": REVISION, "ready": not lagging}
        assert "token" not in result.text and "fixture" not in result.text
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        fake.shutdown()
        fake.server_close()
