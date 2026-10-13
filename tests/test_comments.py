"""Exercise the comments CLI with Google's HTTP client and a real Chromium UI."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from support import CONFIG, LAUNCHER, YouTube, _clip, _free_port, provider, write_token

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
                f'<ytd-comment-thread-renderer><a id="author-text">@JonathanBlowClips</a><p id="content-text">{FOLLOW_COMMENT}</p><p id="pin-label">{pinned}</p><button aria-label="Action menu" onclick="document.querySelector(\'ytd-popup-container\').innerHTML=\'<button onclick=showDialog()>{action}</button>\'">Actions</button></ytd-comment-thread-renderer>'
                if self.posted
                else ""
            )
            self.html(f"""<button aria-label="Account menu" onclick="document.querySelector('ytd-popup-container').innerHTML='<p>@JonathanBlowClips</p>'">Account</button>
<ytd-popup-container></ytd-popup-container>
<ytd-comments id="comments"><button onclick="document.querySelector('ytd-popup-container').innerHTML='<button>Newest first</button>'">Sort by</button>
<button>Add a comment...</button><div id="contenteditable-root" contenteditable="true"></div><button onclick="post()">Comment</button>{row}</ytd-comments>
<script>function showDialog() {{ document.querySelector('ytd-popup-container').innerHTML=''; document.body.insertAdjacentHTML('beforeend','<ytd-confirm-dialog-renderer><button onclick="pin()">Pin</button></ytd-confirm-dialog-renderer>'); }}
async function pin() {{ const response=await fetch('/pin',{{method:'POST'}}); const data=await response.json(); if(data.pinned) document.querySelector('#pin-label').textContent='Pinned by @JonathanBlowClips'; }}
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
            # A real pin request is asynchronous; verification must not reload
            # until its result is visible, even with a slower provider.
            time.sleep(0.2)
            if not self.fail_pin:
                type(self).pins += 1
            self.reply({"pinned": bool(self.pins)})
        else:
            self.reply({}, 404)


COMMENT_LAUNCHER = (
    LAUNCHER.split("from clipy.mcp_server import main")[0]
    + """
import requests
from playwright.sync_api import Browser, Page, Locator
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
# The fixture serves comments immediately; keep absence checks bounded while
# retaining asynchronous pin verification in a real browser.
original_wait = Locator.wait_for
def wait(self, **kwargs):
    kwargs['timeout'] = min(kwargs.get('timeout', 1500), 500)
    return original_wait(self, **kwargs)
Locator.wait_for = wait
import clipy.comments as comments
original_comment_and_pin = comments.comment_and_pin
def traced_comment_and_pin(*args, **kwargs):
    try:
        return original_comment_and_pin(*args, **kwargs)
    except Exception:
        import traceback
        traceback.print_exc()
        raise
comments.comment_and_pin = traced_comment_and_pin
from clipy.cli import main
main()
"""
)


def test_comment_scheduler_resumes_failed_pin_without_posting_twice(tmp_path):
    channel = tmp_path / "channels/blow"
    (tmp_path / "config.toml").write_text(CONFIG)
    item = _clip(
        channel,
        "aaaaaaaaaaa",
        "01",
        status="uploaded",
        youtube_id="new-upload",
        follow_comment_pending=True,
        publish_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    queue = channel / "upload_queue.json"
    queue.write_text(json.dumps([item]))
    write_token(channel)
    Comments.public_checks = Comments.posted = Comments.pins = 0
    Comments.privacy = "public"
    Comments.fail_pin = True
    with provider(Comments) as endpoint, sync_playwright() as p:
        port = _free_port()
        browser = p.chromium.launch(args=[f"--remote-debugging-port={port}"])
        browser.new_context()
        env = {
            **os.environ,
            "CLIPY_CONFIG": str(tmp_path / "config.toml"),
            "CLIPY_DATA_DIR": str(tmp_path),
            "FAKE_GOOGLE_URL": endpoint,
            "CLIPY_STUDIO_CDP": f"http://127.0.0.1:{port}",
        }
        log = tmp_path / "scheduler.log"
        with log.open("w") as output:
            worker = subprocess.Popen(
                [sys.executable, "-c", COMMENT_LAUNCHER, "comments-serve"],
                env=env,
                stdout=output,
                stderr=output,
            )

        def until(check):
            deadline = time.monotonic() + 10
            while not check():
                assert worker.poll() is None and time.monotonic() < deadline, (
                    log.read_text()
                )
                time.sleep(0.05)

        try:
            until(lambda: "Next scheduled comment attempt" in log.read_text())
            assert Comments.public_checks == 0
            item["publish_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            replacement = queue.with_suffix(".tmp")
            replacement.write_text(json.dumps([item]))
            replacement.replace(queue)
            state = channel / "follow-comments.json"
            until(
                lambda: (
                    state.exists()
                    and json.loads(state.read_text()).get("new-upload", {}).get("error")
                )
            )
            assert Comments.posted == 1 and Comments.pins == 0
            worker.terminate()
            worker.wait(timeout=5)
            Comments.fail_pin = False
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, "-c", COMMENT_LAUNCHER, "comments", "blow"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                assert result.returncode == 0, result.stderr
            assert Comments.posted == 1 and Comments.pins == 1
            assert json.loads(state.read_text())["new-upload"]["pinned_at"]
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=5)
            browser.close()
