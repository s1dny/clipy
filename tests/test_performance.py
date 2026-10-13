"""Drive Analytics, counter snapshots and Studio import through real HTTP clients."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from test_mcp_server import CONFIG, ROOT, TOKEN, _free_port, call, data, image
from test_upload_recovery import LAUNCHER, YouTube


class Metrics(YouTube):
    studio_bootstraps = 0
    studio_calls = 0
    studio_wrong_channel = False
    monetized = True

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        path = urlsplit(self.path).path
        if path == "/json/version":
            self.reply({"User-Agent": "Fixture browser"})
        elif path.startswith("/channel/"):
            type(self).studio_bootstraps += 1
            version = str(type(self).studio_bootstraps)
            config = {
                "INNERTUBE_API_KEY": "fixture-key",
                "INNERTUBE_CONTEXT_CLIENT_NAME": 62,
                "INNERTUBE_CONTEXT_CLIENT_VERSION": version,
                "INNERTUBE_CONTEXT": {"client": {"clientVersion": version}},
                "DELEGATION_CONTEXT": {"externalChannelId": "channel-owned"},
                "INNERTUBE_CONTEXT_SERIALIZED_DELEGATION_CONTEXT": "fixture-delegation",
                "SESSION_INDEX": 0,
            }
            body = ("<script>ytcfg.set(" + json.dumps(config) + ");</script>").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/thumbnail-max.jpg":
            self.reply({}, 404)
        elif path == "/thumbnail-high.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write((ROOT / "thumbnails/blow.jpg").read_bytes())
        elif "metrics" in query:
            metrics = query["metrics"][0].split(",")
            dimension = query.get("dimensions", [""])[0]
            assert not ("estimatedRevenue" in metrics and "views" in metrics)
            day = (
                datetime.now(UTC).date()
                - timedelta(days=4 if "estimatedRevenue" in metrics else 2)
            ).isoformat()
            values = {
                "views": 42,
                "estimatedRevenue": 1.25,
                "estimatedMinutesWatched": 12,
                "averageViewDuration": 17,
                "averageViewPercentage": 20,
                "subscribersGained": 3,
                "subscribersLost": 1,
            }
            names = [dimension, *metrics]
            self.reply(
                {
                    "columnHeaders": [{"name": n} for n in names],
                    "rows": [
                        [
                            day if dimension == "day" else "owned-video",
                            *[values[n] for n in metrics],
                        ]
                    ],
                }
            )
        elif path.endswith("/jobs"):
            self.reply(
                {"jobs": [{"id": "reach", "reportTypeId": "channel_reach_basic_a1"}]}
            )
        elif path.endswith("/reports"):
            self.reply({"reports": []})
        elif path.endswith("/channels"):
            self.reply(
                {
                    "items": [
                        {
                            "id": "channel-owned",
                            "status": {
                                "isChannelMonetizationEnabled": type(self).monetized
                            },
                            "snippet": {"title": "Test Channel"},
                            "statistics": {"subscriberCount": "11200"},
                            "contentDetails": {
                                "relatedPlaylists": {"uploads": "uploads"}
                            },
                        }
                    ]
                }
            )
        else:
            super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path != "/youtubei/v1/yta_web/get_cards":
            return super().do_POST()
        query = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        channel_totals = query["screenConfig"]["entity"] == {
            "channelId": "channel-owned"
        }
        if channel_totals:
            date_range = query["screenConfig"]["timePeriod"]["dateIdRange"]
            first = date.fromisoformat(str(date_range["inclusiveStart"]))
            last = date.fromisoformat(str(date_range["exclusiveEnd"]))
            assert (last - first).days == 70
        else:
            assert query["screenConfig"]["entity"] == {"videoId": "owned-video"}
            assert (
                query["screenConfig"]["timePeriod"]["timePeriodType"]
                == "ANALYTICS_TIME_PERIOD_TYPE_SINCE_PUBLISH"
            )
        requested = {
            m["metric"]
            for m in query["cardConfigs"][0]["keyMetricCardConfig"]["metricTabConfigs"]
        }
        if channel_totals:
            assert ("TOTAL_ESTIMATED_EARNINGS" in requested) == type(self).monetized
        assert query["screenConfig"]["currency"] == "USD"
        assert (
            query["context"]["user"]["delegationContext"]["externalChannelId"]
            == "channel-owned"
        )
        assert (
            query["context"]["user"]["serializedDelegationContext"]
            == "fixture-delegation"
        )
        assert self.headers["X-Youtube-Client-Version"] == str(
            type(self).studio_bootstraps
        )
        assert self.headers["X-Youtube-Delegation-Context"] == "fixture-delegation"
        assert "SAPISID=fixture-session" in self.headers["Cookie"]
        stamp, digest = self.headers["Authorization"].split()[1].split("_")
        assert abs(int(time.time()) - int(stamp)) < 10
        assert (
            digest
            == hashlib.sha1(
                f"{stamp} fixture-session https://studio.youtube.com".encode()
            ).hexdigest()
        )
        type(self).studio_calls += 1
        if type(self).studio_calls == 1:
            self.reply({"error": {"code": 401}}, 401)
            return
        values = {
            "EXTERNAL_VIEWS": 9401,
            "EXTERNAL_WATCH_TIME": 1275840000,
            "SUBSCRIBERS_NET_CHANGE": -2,
            "TOTAL_ESTIMATED_EARNINGS": 2468,
            "VIDEO_THUMBNAIL_IMPRESSIONS": 34567,
            "VIDEO_THUMBNAIL_IMPRESSIONS_VTR": 6.04,
        }
        channel = (
            "wrong-channel" if type(self).studio_wrong_channel else "channel-owned"
        )
        from zoneinfo import ZoneInfo

        yesterday = datetime.now(ZoneInfo("Australia/Sydney")).date() - timedelta(
            days=1
        )
        stamp = int(
            datetime.combine(
                yesterday, datetime.min.time(), ZoneInfo("America/Los_Angeles")
            ).timestamp()
            * 1000
        )

        def content(metric, value):
            body = {
                "metric": metric,
                "total": value,
                "anomalies": [{"type": "ANALYTICS_ANOMALY_TYPE_DATA_ESTIMATED"}],
            }
            if channel_totals:
                body["mainSeries"] = {
                    "datums": [{"x": stamp, "y": value}],
                    "timeUnit": "TIME_PERIOD_UNIT_DAYS",
                }
                if metric == "TOTAL_ESTIMATED_EARNINGS":
                    body["mainSeries"]["datums"] = [
                        {"x": stamp - 86400000, "y": value},
                        {"x": stamp, "y": 0},
                    ]
                    body["anomalies"] = [
                        {
                            "impact": "ANALYTICS_ANOMALY_IMPACT_DATA_MISSING",
                            "startDate": stamp,
                            "endDate": stamp + 86400000,
                        }
                    ]
            return body

        self.reply(
            {
                "cards": [
                    {
                        "config": {"topEntitySelection": {"channelId": channel}},
                        "keyMetricCardData": {
                            "keyMetricTabs": [
                                {
                                    "primaryContent": content(metric, value),
                                }
                                for metric, value in values.items()
                                if metric in requested
                            ]
                        },
                    }
                ]
            },
            **{
                "Set-Cookie": "SIDCC=rotated; Domain=.youtube.com; Path=/; Secure; HttpOnly"
            },
        )


@pytest.fixture
def metric_server(tmp_path):
    now = datetime.now(UTC)
    Metrics.studio_bootstraps = 0
    Metrics.studio_calls = 0
    Metrics.studio_wrong_channel = False
    Metrics.monetized = True
    channel = tmp_path / "channels/blow"
    channel.mkdir(parents=True)
    (tmp_path / "config.toml").write_text(CONFIG.split("[channels.science]")[0])
    (channel / ".youtube_token.json").write_text(
        json.dumps(
            {
                "token": "test",
                "refresh_token": "unused",
                "client_id": "test",
                "client_secret": "test",
                "expiry": (now + timedelta(hours=1)).isoformat(),
                "scopes": [
                    "https://www.googleapis.com/auth/" + s
                    for s in [
                        "youtube",
                        "youtube.upload",
                        "yt-analytics.readonly",
                        "yt-analytics-monetary.readonly",
                    ]
                ],
            }
        )
    )
    old = {
        "views": 100,
        "likes": 5,
        "comments": 1,
        "title": "Example",
        "privacy": "public",
        "publish_time": (now - timedelta(days=6)).isoformat(),
    }
    (channel / "performance.json").write_text(
        json.dumps(
            [
                {
                    "observed_at": (now - timedelta(hours=24)).isoformat(),
                    "subscribers": 11100,
                    "videos": {"owned-video": old},
                }
            ]
        )
    )
    Metrics.existing = [
        {
            "id": "owned-video",
            "snippet": {
                "title": "Example",
                "categoryId": "28",
                "description": "Original: example",
                "publishedAt": old["publish_time"],
            },
            "status": {"privacyStatus": "public", "uploadStatus": "processed"},
            "contentDetails": {"duration": "PT10M"},
            "statistics": {"viewCount": "140", "likeCount": "7", "commentCount": "2"},
        }
    ]
    fake = ThreadingHTTPServer(("127.0.0.1", 0), Metrics)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    Metrics.existing[0]["snippet"]["thumbnails"] = {
        "maxres": {"url": f"http://127.0.0.1:{fake.server_port}/thumbnail-max.jpg"},
        "high": {"url": f"http://127.0.0.1:{fake.server_port}/thumbnail-high.jpg"},
    }
    port = _free_port()
    env = {
        **os.environ,
        "CLIPY_CONFIG": str(tmp_path / "config.toml"),
        "CLIPY_DATA_DIR": str(tmp_path),
        "CLIPY_MCP_TOKEN": TOKEN,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "FAKE_GOOGLE_URL": f"http://127.0.0.1:{fake.server_port}",
    }
    proc = subprocess.Popen([sys.executable, "-c", LAUNCHER], env=env)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if requests.get(base + "/healthz", timeout=1).ok:
                    break
            except requests.ConnectionError:
                pass
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        yield (base, tmp_path), env
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        fake.shutdown()
        fake.server_close()


def test_reports_separate_fresh_engagement_revenue_and_current_counts(metric_server):
    server, _ = metric_server
    report = data(call(server, "channel_report", {"rank_by": "watch_hours"}))
    assert report["data_through"] > report["revenue_data_through"]
    assert report["totals"]["watch_hours"] == 0.2
    assert report["videos"][0]["rpm_usd"] is None
    assert report["daily"][0]["revenue_usd"] is None
    recent = data(call(server, "recent_performance"))
    assert recent["videos"][0]["counter_changes"]["24"]["views"] == 40
    assert recent["videos"][0]["counter_changes"]["48"]["views"] is None
    assert recent["subscribers"] == 11200
    response = requests.get(
        server[0] + "/stats", headers={"Authorization": "Bearer " + TOKEN}, timeout=10
    )
    assert response.ok
    assert "Analytics unavailable" in response.text
    assert "Views: **42**" not in response.text


def test_studio_import_retains_provenance_and_rejects_wrong_channel(metric_server):
    server, env = metric_server
    reading = {
        "video_id": "owned-video",
        "observed_at": datetime.now(UTC).isoformat(),
        "url": "https://studio.youtube.com/video/owned-video/analytics/tab-reach_viewers/period-default",
        "period": "since_published",
        "approximate": True,
        "metrics": {"impressions": 34500, "ctr_percent": 6, "revenue_usd": None},
    }
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            LAUNCHER.replace(
                "from clipy.mcp_server import main", "from clipy.cli import main"
            ),
            "studio-import",
            "blow",
        ],
        env=env,
        input=json.dumps([reading]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr
    recent = data(call(server, "recent_performance"))
    studio = recent["studio"]["videos"][0]
    assert studio["source"] == "visible YouTube Studio UI"
    assert studio["approximate"] and not studio["stale"]
    assert studio["metrics"]["revenue_usd"] is None
    reading["video_id"] = "another-channel"
    bad = subprocess.run(
        [
            sys.executable,
            "-c",
            LAUNCHER.replace(
                "from clipy.mcp_server import main", "from clipy.cli import main"
            ),
            "studio-import",
            "blow",
        ],
        env=env,
        input=json.dumps([reading]),
        text=True,
        capture_output=True,
        check=False,
    )
    assert bad.returncode != 0
    assert "does not belong" in bad.stderr


def test_private_video_preview_uses_api_renditions_when_maxres_is_missing(
    metric_server,
):
    server, _ = metric_server
    preview = image(call(server, "view_thumbnail", {"video_id": "owned-video"}))
    assert preview.size == (1920, 1080)


@pytest.mark.parametrize(
    "wrong_channel,monetized", [(False, True), (False, False), (True, True)]
)
def test_direct_studio_collection_refreshes_auth_without_rendering(
    metric_server, wrong_channel, monetized
):
    server, env = metric_server
    Metrics.studio_wrong_channel = wrong_channel
    Metrics.monetized = monetized
    session = server[1] / "private-session.json"
    session.write_text(
        json.dumps(
            [
                {
                    "name": "SID",
                    "value": "fixture-session",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "expires": -1,
                },
                {
                    "name": "SAPISID",
                    "value": "fixture-session",
                    "domain": ".youtube.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "expires": -1,
                },
            ]
        )
    )
    session.chmod(0o600)
    collector = (
        LAUNCHER.split("from clipy.mcp_server import main")[0]
        + """
import copy, requests
from requests.adapters import HTTPAdapter
from urllib.parse import urlsplit
import clipy.studio_collect as studio
# Real HTTP, cookies, authentication hashes, JSON parsing and persistence.
# Only the destination socket changes to the fixture's local server.
class FixtureTransport(HTTPAdapter):
    def send(self, request, **kwargs):
        redirected = copy.copy(request)
        parsed = urlsplit(request.url)
        redirected.url = os.environ['FAKE_GOOGLE_URL'] + parsed.path + ('?' + parsed.query if parsed.query else '')
        response = super().send(redirected, **kwargs)
        response.url = request.url
        response.request = request
        return response
original_session_init = requests.Session.__init__
def session_init(self):
    original_session_init(self)
    self.mount('https://studio.youtube.com/', FixtureTransport())
requests.Session.__init__ = session_init
def no_browser():
    raise AssertionError('Direct collection attempted to render browser pages')
studio.sync_playwright = no_browser
from clipy.cli import main
import sys
sys.argv = ['clipy', 'studio-collect', 'blow']
main()
"""
    )
    env = {
        **env,
        "CLIPY_STUDIO_SESSION": str(session),
        "CLIPY_STUDIO_CDP": env["FAKE_GOOGLE_URL"],
    }
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", collector],
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if wrong_channel:
            assert result.returncode == 1, result.stderr
            assert not (server[1] / "channels/blow/studio.json").exists()
        else:
            assert result.returncode == 0, result.stderr
            report = data(call(server, "recent_performance"))
            reading = report["studio"]["videos"][0]
            assert reading["source"] == "YouTube Studio internal analytics API"
            assert reading["currency"] == "USD"
            assert reading["metrics"] == {
                "views": 9401,
                "watch_hours": 354.4,
                "subscribers": -2,
                "revenue_usd": 2.468,
                "impressions": 34567,
                "ctr_percent": 6.04,
            }
            assert reading["approximate"] and reading["period"] == "since_published"
            header = requests.get(
                server[0] + "/stats",
                headers={"Authorization": "Bearer " + TOKEN},
                timeout=10,
            ).text
            assert "Views: **9,401**" in header
            assert "Subscribers: **-2**" in header
            if monetized:
                assert "Earnings: **$2.47**" in header
            else:
                assert "Earnings: unavailable" in header
                assert "**$2.47**" not in header
            assert "YouTube Studio estimates" not in header
            assert "(through " not in header
            path = server[1] / "channels/blow/studio-channel.json"
            snapshot = json.loads(path.read_text())
            assert snapshot["currency"] == "USD"
            assert snapshot["daily"][max(snapshot["daily"])].get("revenue_usd") is None
            snapshot["observed_at"] = (
                datetime.now(UTC) - timedelta(hours=3)
            ).isoformat()
            path.write_text(json.dumps(snapshot))
            stale = requests.get(
                server[0] + "/stats",
                headers={"Authorization": "Bearer " + TOKEN},
                timeout=10,
            ).text
            assert "over two hours old" in stale and "Views: **9,401**" not in stale
            cookies = json.loads(session.read_text())
            assert any(
                c["name"] == "SIDCC" and c["value"] == "rotated" and c["httpOnly"]
                for c in cookies
            )
            assert session.stat().st_mode & 0o777 == 0o600
    assert Metrics.studio_bootstraps == 3


def test_tuning_records_before_after_and_observed_baseline(metric_server):
    server, _ = metric_server
    result = call(
        server, "update_video", {"video_id": "owned-video", "title": "A better title"}
    )
    assert not result.is_error
    changes = json.loads((server[1] / "channels/blow/experiments.json").read_text())
    assert changes[-1]["before"]["title"] == "Example"
    assert changes[-1]["after"]["title"] == "A better title"
    assert changes[-1]["baseline"]["counters"]["views"] == 140
    assert changes[-1]["baseline"]["observed_at"] <= changes[-1]["changed_at"]
