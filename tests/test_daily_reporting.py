"""Run the daily shell agent and real HTTP stats endpoint against fake providers."""

import json
import os
import shlex
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
import requests
from test_mcp_server import CONFIG, ROOT, TOKEN, _free_port


class Provider(BaseHTTPRequestHandler):
    messages: ClassVar[list[dict]] = []

    def do_GET(self):
        if self.path == "/stats":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Views: **42**")
            return
        assert self.headers["Authorization"] == "Bearer science-token"
        day = (datetime.now(UTC).date() - timedelta(days=3)).isoformat()
        self.respond(
            {
                "columnHeaders": [
                    {"name": name}
                    for name in (
                        "day",
                        "views",
                        "estimatedRevenue",
                        "subscribersGained",
                        "subscribersLost",
                    )
                ],
                "rows": [[day, 42, 1.25, 3, 1]],
            }
        )

    def do_PUT(self):
        self.messages.append(
            json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        )
        self.respond({"event_id": "$test-report"})

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.respond(
            {"error": "invalid_grant", "error_description": "Token revoked."},
            status=400,
        )

    def respond(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def provider():
    Provider.messages = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


FAKE_CODEX = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv
assert args[args.index('--model') + 1] == 'gpt-6.1-sol'
work = pathlib.Path(args[args.index('--cd') + 1])
count = work / 'attempts'
n = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(n))
def emit(event): print(json.dumps(event))
if work.name == 'observer':
    emit({'type':'turn.failed','error':{'message':'You have hit your usage limit.'}})
    sys.exit(1)
if os.environ['FAKE_MODE'] == 'partial':
    emit({'type':'item.completed','item':{'type':'mcp_tool_call','server':'clipy','tool':'write_notes','status':'completed'}})
    emit({'type':'item.completed','item':{'type':'agent_message','text':'Notebook updated.'}})
    emit({'type':'turn.failed','error':{'message':'You have hit your usage limit.'}})
    sys.exit(1)
if os.environ['FAKE_MODE'] == 'quota' or n == 1:
    emit({'type':'error','message':'You have hit your usage limit.'})
    emit({'type':'turn.failed','error':{'message':'You have hit your usage limit.'}})
    sys.exit(1)
emit({'type':'item.completed','item':{'type':'mcp_tool_call','server':'clipy','tool':'read_notes','status':'completed'}})
emit({'type':'item.completed','item':{'type':'agent_message','text':'Channel run completed.'}})
emit({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':10,'cached_input_tokens':80,'reasoning_output_tokens':5}})
"""


@pytest.mark.parametrize(
    "mode,attempts,exit_code", [("recover", 2, 0), ("quota", 3, 1), ("partial", 1, 1)]
)
def test_daily_run_reports_failures_and_retries_only_before_tools(
    tmp_path, provider, mode, attempts, exit_code
):
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "codex").write_text(FAKE_CODEX)
    (binary / "codex").chmod(0o755)
    (binary / "python").write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
    )
    (binary / "python").chmod(0o755)
    login = tmp_path / "codex"
    login.mkdir()
    (login / "auth.json").write_text("{}")
    (tmp_path / "matrix-room").write_text("!test:local")
    work = tmp_path / "work"
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "agent/run.sh"), "blow"],
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "CODEX_HOME": str(login),
            "CODEX_MODEL": "gpt-6.1-sol",
            "CLIPY_AGENT_DIR": str(ROOT / "agent"),
            "CLIPY_AGENT_WORK_DIR": str(work),
            "CLIPY_QUOTA_RETRY_SECONDS": "0",
            "CLIPY_MCP_TOKEN": TOKEN,
            "CLIPY_MCP_URL": provider + "/mcp",
            "CLIPY_DATA_DIR": str(tmp_path),
            "MATRIX_HOMESERVER": provider,
            "MATRIX_ACCESS_TOKEN": "fake",
            "FAKE_MODE": mode,
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert int((work / "blow/attempts").read_text()) == attempts
    assert len(Provider.messages) == 1, result.stdout + result.stderr
    report = Provider.messages[0]
    assert report["body"].startswith("Views: **42**")
    assert "Owner actions: could not be checked automatically" in report["body"]
    assert "cached" not in report["body"] and "reasoning" not in report["body"]
    assert "Here are the channels" not in report["body"]
    assert "<strong>42</strong>" in report["formatted_body"]
    assert "Channel run completed." not in report["body"]
    assert "Notebook updated." not in report["body"]


def test_stats_endpoint_keeps_healthy_channel_when_another_needs_login(
    tmp_path, provider
):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    token = tmp_path / "channels/science/.youtube_token.json"
    token.parent.mkdir(parents=True)
    token.write_text(
        json.dumps(
            {
                "token": "science-token",
                "refresh_token": "unused",
                "client_id": "test",
                "client_secret": "test",
                "expiry": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "scopes": [
                    "https://www.googleapis.com/auth/youtube",
                    "https://www.googleapis.com/auth/youtube.upload",
                    "https://www.googleapis.com/auth/yt-analytics.readonly",
                    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
                ],
            }
        )
    )
    (token.parent / "studio-channel.json").write_text(
        json.dumps(
            {
                "observed_at": datetime.now(UTC).isoformat(),
                "currency": "USD",
                "daily": {
                    (datetime.now(UTC).date() - timedelta(days=1)).isoformat(): {
                        "views": 42,
                        "subscribers": 2,
                        "revenue_usd": 1.25,
                    }
                },
            }
        )
    )
    # Use Google's actual discovery client and HTTP requests, with only its
    # provider URL redirected to our local fake. Production has no test switch.
    launcher = """
import json, os
from googleapiclient import discovery
from google.oauth2 import credentials
credentials._GOOGLE_OAUTH2_TOKEN_ENDPOINT = os.environ['FAKE_GOOGLE_URL'] + '/token'
original = discovery._retrieve_discovery_doc
def local_document(*args, **kwargs):
    document = json.loads(original(*args, **kwargs))
    document['rootUrl'] = os.environ['FAKE_GOOGLE_URL'] + '/'
    return json.dumps(document)
discovery._retrieve_discovery_doc = local_document
from clipy.mcp_server import main
main()
"""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", launcher],
        env={
            **os.environ,
            "CLIPY_CONFIG": str(config),
            "CLIPY_DATA_DIR": str(tmp_path),
            "CLIPY_MCP_TOKEN": TOKEN,
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "FAKE_GOOGLE_URL": provider,
        },
    )
    try:
        import time

        deadline = time.monotonic() + 60
        while True:
            try:
                if requests.get(f"http://127.0.0.1:{port}/healthz", timeout=1).ok:
                    break
            except requests.ConnectionError:
                pass
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        url = f"http://127.0.0.1:{port}/stats"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        response = requests.get(url, headers=headers, timeout=10)
        assert response.status_code == 200
        assert "Totals include available channels only" in response.text
        assert "Views: **42**" in response.text
        assert "Earnings: **$1.25**" in response.text
        assert "Subscribers: **+2**" in response.text
        assert "Science: **42**" in response.text
        assert (
            "JBlow: unavailable (reconnect YouTube with clipy auth blow)"
            in response.text
        )
        token.unlink()
        response = requests.get(url, headers=headers, timeout=10)
        assert response.status_code == 200
        assert "Analytics unavailable" in response.text
        assert "clipy auth science" in response.text
    finally:
        proc.terminate()
        proc.wait(timeout=10)
