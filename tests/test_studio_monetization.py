"""Enable ads and surface reauthentication through the real channel MCP server."""

import json
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from support import CONFIG, app, data, provider, text, write_token
from support import call as mcp_call
from test_performance import Metrics


def call(*args, **kwargs):
    # Playwright's synchronous driver owns this thread's event loop.
    with ThreadPoolExecutor(max_workers=1) as client:
        return client.submit(mcp_call, *args, **kwargs).result(timeout=30)


class Studio(Metrics):
    ads_on = False
    challenge = False
    writes = 0

    def do_GET(self):
        if urlsplit(self.path).path == "/channel/channel-owned":
            body = b"""<ytcp-app></ytcp-app><script>
            customElements.define('ytcp-app', class extends HTMLElement {
              connectedCallback() {
                this.currentChannelId='channel-owned';
                this.attestationManagerNetwork={attApi:{gapiVersion:'youtubei/v1'},baseContextFactory:()=>({user:{delegationContext:{externalChannelId:'channel-owned'}}})};
                this.secureRequestService={makeRequest:async r=>(await fetch(r.path,{method:'POST',headers:{'Content-Type':'application/json','X-Fixture-Studio-Proof':'native-runtime'},body:JSON.stringify(r.body)})).json()};
              }
            });
            </script>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def do_POST(self):
        endpoint = urlsplit(self.path).path
        if not endpoint.endswith(("/get_creator_videos", "/metadata_update")):
            return super().do_POST()
        assert self.headers.get("X-Fixture-Studio-Proof") == "native-runtime"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert (
            body["context"]["user"]["delegationContext"]["externalChannelId"]
            == "channel-owned"
        )
        if endpoint.endswith("/metadata_update"):
            type(self).writes += 1
            assert body["encryptedVideoId"] == "owned-video"
            if self.challenge:
                self.reply(
                    {
                        "responseContext": {
                            "webResponseContextExtensionData": {
                                "challenge": {
                                    "type": "CHALLENGE_PROMPT_TYPE_AUTHENTICATE"
                                }
                            }
                        }
                    }
                )
                return
            type(self).ads_on = body["monetizationSettings"]["newMonetizeWithAds"]
            self.reply({"monetizationSettings": {"success": True}})
        else:
            assert body["videoIds"] == ["owned-video"]
            self.reply(
                {
                    "videos": [
                        {
                            "videoId": "owned-video",
                            "channelId": "channel-owned",
                            "monetization": {
                                "adMonetization": {
                                    "userSetMonetization": "VIDEO_USER_SET_MONETIZATION_ON"
                                    if self.ads_on
                                    else "VIDEO_USER_SET_MONETIZATION_OFF",
                                    "effectiveStatus": "VIDEO_MONETIZING_STATUS_MONETIZING"
                                    if self.ads_on
                                    else "VIDEO_MONETIZING_STATUS_NOT_MONETIZING_OFF",
                                }
                            },
                        }
                    ]
                }
            )


def test_ads_write_requires_verified_readback_and_surfaces_authentication(tmp_path):
    Studio.ads_on = Studio.challenge = False
    Studio.writes = 0
    Studio.existing = [{"id": "owned-video", "snippet": {"channelId": "channel-owned"}}]
    write_token(tmp_path / "channels/blow")
    (tmp_path / "config.toml").write_text(CONFIG.split("[channels.science]")[0])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (
        sync_playwright() as playwright,
        provider(Studio) as endpoint,
        app(tmp_path, FAKE_GOOGLE_URL=endpoint) as (server, env),
    ):
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "browser"),
            headless=True,
            args=[f"--remote-debugging-port={port}"],
        )
        worker_env = {**env, "CLIPY_STUDIO_CDP": f"http://127.0.0.1:{port}"}
        worker_log = (tmp_path / "worker.log").open("w")
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os; import clipy.studio_browser as b; b.ORIGIN=os.environ['FAKE_GOOGLE_URL']; b.serve()",
            ],
            env=worker_env,
            stdout=worker_log,
            stderr=worker_log,
        )
        try:
            read = {
                "endpoint": "creator/get_creator_videos",
                "payload": {
                    "videoIds": ["owned-video"],
                    "mask": {
                        "videoId": True,
                        "channelId": True,
                        "monetization": {"all": True},
                    },
                    "criticalRead": True,
                },
            }
            before = data(call(server, "studio_api_request", read))["response"][
                "videos"
            ][0]
            assert before["monetization"]["adMonetization"][
                "userSetMonetization"
            ].endswith("_OFF")
            write = {
                "endpoint": "video_manager/metadata_update",
                "payload": {
                    "encryptedVideoId": "owned-video",
                    "monetizationSettings": {"newMonetizeWithAds": True},
                },
                "observation": before,
            }
            Studio.challenge = True
            failure = call(server, "studio_api_request", write)
            assert failure.is_error and "challenge" in text(failure)
            assert Studio.writes == 1 and Studio.ads_on is False
            audits = [
                json.loads(p.read_text())
                for p in (tmp_path / "channels/blow/studio-api").glob("*.json")
            ]
            assert any(
                a["status"] == "unconfirmed" and a["observation"] == before
                for a in audits
            )
            assert not call(server, "studio_api_request", read).is_error
            Studio.challenge = False
            assert not call(server, "studio_api_request", write).is_error
            after = data(call(server, "studio_api_request", read))["response"][
                "videos"
            ][0]
            assert (
                after["monetization"]["adMonetization"]["effectiveStatus"]
                == "VIDEO_MONETIZING_STATUS_MONETIZING"
            )
            denied = call(
                server,
                "studio_api_request",
                write
                | {
                    "payload": {
                        "encryptedVideoId": "another-video",
                        "monetizationSettings": {"newMonetizeWithAds": True},
                    }
                },
            )
            assert denied.is_error and Studio.writes == 2
        finally:
            worker.terminate()
            worker.wait(timeout=5)
            worker_log.close()
            context.close()
