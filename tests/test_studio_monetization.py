"""Enable ads and surface reauthentication through the real channel MCP server."""

import json
from urllib.parse import urlsplit

from support import CONFIG, LAUNCHER, app, call, data, provider, text, write_token
from test_performance import Metrics


class Studio(Metrics):
    ads_on = False
    challenge = False
    writes = 0

    def do_POST(self):
        endpoint = urlsplit(self.path).path
        if not endpoint.endswith(("/get_creator_videos", "/metadata_update")):
            return super().do_POST()
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
    session = tmp_path / "cookies.json"
    session.write_text(
        json.dumps(
            [
                {
                    "name": "SAPISID",
                    "value": "fixture",
                    "domain": ".youtube.com",
                    "path": "/",
                    "secure": True,
                    "expires": -1,
                }
            ]
        )
    )
    launcher = LAUNCHER.replace(
        "from clipy.mcp_server import main",
        "import clipy.studio_collect as sc; sc.ORIGIN = os.environ['FAKE_GOOGLE_URL']\nfrom clipy.mcp_server import main",
    )
    with (
        provider(Studio) as endpoint,
        app(
            tmp_path,
            launcher=launcher,
            FAKE_GOOGLE_URL=endpoint,
            CLIPY_STUDIO_SESSION=str(session),
            CLIPY_STUDIO_CDP=endpoint,
        ) as (server, _),
    ):
        read = {
            "endpoint": "creator/get_creator_videos",
            "payload": {
                "videoIds": ["owned-video"],
                "mask": {"monetization": {"all": True}},
                "criticalRead": True,
            },
        }
        before = data(call(server, "studio_api_request", read))["response"]["videos"][0]
        assert before["monetization"]["adMonetization"]["userSetMonetization"].endswith(
            "_OFF"
        )
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
        assert failure.is_error and "authentication challenge" in text(failure)
        assert Studio.writes == 1 and Studio.ads_on is False
        audits = [
            json.loads(p.read_text())
            for p in (tmp_path / "channels/blow/studio-api").glob("*.json")
        ]
        assert any(
            a["status"] == "unconfirmed" and a["observation"] == before for a in audits
        )
        assert not call(server, "studio_api_request", read).is_error
        Studio.challenge = False
        assert not call(server, "studio_api_request", write).is_error
        after = data(call(server, "studio_api_request", read))["response"]["videos"][0]
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
