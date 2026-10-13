"""Run the daily shell agent and real HTTP stats endpoint against fake providers."""

import json
import os
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from support import ROOT, TOKEN


class Provider(BaseHTTPRequestHandler):
    messages: ClassVar[list[dict]] = []

    def do_GET(self):
        if self.path == "/stats":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Views: **42**")
            return
        self.send_error(404)

    def do_PUT(self):
        self.messages.append(
            json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        )
        self.respond({"event_id": "$test-report"})

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
if n == 1:
    emit({'type':'error','message':'You have hit your usage limit.'})
    emit({'type':'turn.failed','error':{'message':'You have hit your usage limit.'}})
    sys.exit(1)
emit({'type':'item.completed','item':{'type':'mcp_tool_call','server':'clipy','tool':'read_notes','status':'completed'}})
emit({'type':'item.completed','item':{'type':'agent_message','text':'Channel run completed.'}})
emit({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':10,'cached_input_tokens':80,'reasoning_output_tokens':5}})
"""


@pytest.mark.parametrize(
    "mode,attempts,exit_code", [("recover", 2, 0), ("partial", 1, 1)]
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
    assert report["formatted_body"]
