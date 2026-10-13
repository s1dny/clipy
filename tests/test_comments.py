"""Exercise the comments CLI with Google's HTTP client and a real Chromium UI."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright
from test_mcp_server import CONFIG, _clip, _free_port
from test_upload_recovery import LAUNCHER, YouTube

from clipy.comments import FOLLOW_COMMENT


class Comments(YouTube):
    privacy = "private"
    public_checks = 0
    posted = 0
    pins = 0
    fail_pin = False

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.endswith("/channels"):
            self.reply({"items": [{"id": "channel-owned"}]})
        elif path.endswith("/videos"):
            type(self).public_checks += 1
            self.reply(
                {
                    "items": [
                        {
                            "id": "new-upload",
                            "status": {"privacyStatus": self.privacy},
                            "snippet": {"channelId": "channel-owned"},
                        }
                    ]
                }
            )
        elif path == "/channel_switcher":
            self.html('<a href="/">@JonathanBlowClips</a>')
        elif path == "/":
            self.html("Selected channel")
        elif path == "/watch":
            pinned = "Pinned by @JonathanBlowClips" if self.pins else ""
            action = "Unpin" if self.pins else "Pin"
            row = (
                f'<ytd-comment-thread-renderer><a id="author-text">@JonathanBlowClips</a><p id="content-text">{FOLLOW_COMMENT}</p><p>{pinned}</p><button aria-label="Action menu" onclick="document.querySelector(\'ytd-popup-container\').innerHTML=\'<button onclick=showDialog()>{action}</button>\'">Actions</button></ytd-comment-thread-renderer>'
                if self.posted
                else ""
            )
            self.html(f"""<button aria-label="Account menu" onclick="document.querySelector('ytd-popup-container').innerHTML='<p>@JonathanBlowClips</p>'">Account</button>
<ytd-popup-container></ytd-popup-container>
<ytd-comments id="comments"><button onclick="document.querySelector('ytd-popup-container').innerHTML='<button>Newest first</button>'">Sort by</button>
<button>Add a comment...</button><div id="contenteditable-root" contenteditable="true"></div><button onclick="post()">Comment</button>{row}</ytd-comments>
<script>function showDialog() {{ document.querySelector('ytd-popup-container').innerHTML=''; document.body.insertAdjacentHTML('beforeend','<ytd-confirm-dialog-renderer><button onclick="pin()">Pin</button></ytd-confirm-dialog-renderer>'); }}
async function pin() {{ await fetch('/pin',{{method:'POST'}}); location.reload(); }}
async function post() {{ await fetch('/post',{{method:'POST',body:document.querySelector('#contenteditable-root').innerText}}); location.reload(); }}</script>""")
        else:
            super().do_GET()

    def html(self, body):
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        path = urlsplit(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if path == "/post":
            assert raw.decode() == FOLLOW_COMMENT
            type(self).posted += 1
            self.reply({})
        elif path == "/pin":
            if not self.fail_pin:
                type(self).pins += 1
            self.reply({})
        else:
            self.reply({}, 404)


COMMENT_LAUNCHER = (
    LAUNCHER.split("from clipy.mcp_server import main")[0]
    + """
import requests
from playwright.sync_api import Browser, Page
original_context = Browser.new_context
def context(self, **kwargs):
    ctx = original_context(self, **kwargs)
    def route(request_route):
        r = request_route.request
        from urllib.parse import urlsplit
        parsed = urlsplit(r.url)
        response = requests.request(r.method, os.environ['FAKE_GOOGLE_URL'] + parsed.path + ('?' + parsed.query if parsed.query else ''), data=r.post_data)
        request_route.fulfill(status=response.status_code, content_type=response.headers.get('Content-Type','text/html'), body=response.content)
    ctx.route('https://www.youtube.com/**', route)
    return ctx
Browser.new_context = context
original_timeout = Page.set_default_timeout
Page.set_default_timeout = lambda self, value: original_timeout(self, 1500)
from clipy.cli import main
main()
"""
)


@pytest.mark.parametrize("scheduled", [False, True])
def test_private_upload_waits_then_posts_and_pins_once_with_retry(tmp_path, scheduled):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    item = _clip(
        tmp_path / "channels/blow",
        "aaaaaaaaaaa",
        "01",
        status="uploaded",
        youtube_id="new-upload",
        follow_comment_pending=True,
    )
    queue = tmp_path / "channels/blow/upload_queue.json"
    queue.write_text(json.dumps([item]))
    (queue.parent / ".youtube_token.json").write_text(
        json.dumps(
            {
                "token": "fixture",
                "refresh_token": "unused",
                "client_id": "fixture",
                "client_secret": "fixture",
                "expiry": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "scopes": [
                    "https://www.googleapis.com/auth/" + s
                    for s in (
                        "youtube",
                        "youtube.upload",
                        "yt-analytics.readonly",
                        "yt-analytics-monetary.readonly",
                    )
                ],
            }
        )
    )
    Comments.public_checks = 0
    Comments.privacy = "private"
    Comments.posted = 0
    Comments.pins = 0
    Comments.fail_pin = False
    fake = ThreadingHTTPServer(("127.0.0.1", 0), Comments)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    with sync_playwright() as p:
        port = _free_port()
        browser = p.chromium.launch(args=[f"--remote-debugging-port={port}"])
        browser.new_context()
        env = {
            **os.environ,
            "CLIPY_CONFIG": str(config),
            "CLIPY_DATA_DIR": str(tmp_path),
            "FAKE_GOOGLE_URL": f"http://127.0.0.1:{fake.server_port}",
            "CLIPY_STUDIO_CDP": f"http://127.0.0.1:{port}",
        }

        def run():
            return subprocess.run(
                [sys.executable, "-c", COMMENT_LAUNCHER, "comments", "blow"],
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )

        try:
            if scheduled:
                Comments.privacy = "public"
                item["publish_at"] = (
                    datetime.now(UTC) + timedelta(hours=1)
                ).isoformat()
                queue.write_text(json.dumps([item]))
                log = tmp_path / "scheduler.log"
                with log.open("w") as output:
                    worker = subprocess.Popen(
                        [sys.executable, "-c", COMMENT_LAUNCHER, "comments-serve"],
                        env=env,
                        stdout=output,
                        stderr=output,
                    )
                try:
                    deadline = time.monotonic() + 15
                    while "Next scheduled comment attempt" not in log.read_text():
                        assert worker.poll() is None and time.monotonic() < deadline, (
                            log.read_text()
                        )
                        time.sleep(0.05)
                    assert Comments.public_checks == 0
                    item["publish_at"] = (
                        datetime.now(UTC) + timedelta(seconds=3)
                    ).isoformat()
                    replacement = queue.with_suffix(".tmp")
                    replacement.write_text(json.dumps([item]))
                    replacement.replace(queue)
                    time.sleep(1)
                    assert Comments.public_checks == 0 and Comments.posted == 0
                    state_path = queue.parent / "follow-comments.json"
                    deadline = time.monotonic() + 25
                    while not state_path.exists() or not json.loads(
                        state_path.read_text()
                    ).get("new-upload", {}).get("pinned_at"):
                        assert worker.poll() is None and time.monotonic() < deadline, (
                            log.read_text()
                        )
                        time.sleep(0.05)
                    assert (
                        Comments.public_checks == 1
                        and Comments.posted == 1
                        and Comments.pins == 1
                    )
                    worker.terminate()
                    worker.wait(timeout=5)
                    result = run()
                    assert result.returncode == 0, result.stderr
                    assert Comments.public_checks == 1 and Comments.posted == 1
                finally:
                    if worker.poll() is None:
                        worker.terminate()
                        worker.wait(timeout=5)
            else:
                result = run()
                assert result.returncode == 0, result.stderr
                assert Comments.posted == 0 and Comments.pins == 0
                Comments.privacy = "public"
                Comments.fail_pin = True
                assert run().returncode == 1
                saved = json.loads((queue.parent / "follow-comments.json").read_text())
                assert saved["new-upload"]["posted_at"]
                assert not saved["new-upload"].get("pinned_at")
                Comments.fail_pin = False
                result = run()
                assert result.returncode == 0, result.stderr
                assert Comments.pins == 1 and Comments.posted == 1
                assert run().returncode == 0
                assert Comments.pins == 1 and Comments.posted == 1
                assert json.loads((queue.parent / "follow-comments.json").read_text())[
                    "new-upload"
                ]["pinned_at"]
                assert json.loads(queue.read_text()) == [item]
        finally:
            browser.close()
            fake.shutdown()
            fake.server_close()
