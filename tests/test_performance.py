"""Drive Analytics, counter snapshots and Studio import through real HTTP clients."""

import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from support import (
    CONFIG,
    LAUNCHER,
    ROOT,
    TOKEN,
    YouTube,
    app,
    call,
    data,
    image,
    provider,
    write_token,
)


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
        assert query["screenConfig"]["currency"] == ("AUD" if channel_totals else "USD")
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
            "TOTAL_ESTIMATED_EARNINGS": 3949 if channel_totals else 2468,
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
    Metrics.studio_bootstraps = Metrics.studio_calls = 0
    Metrics.studio_wrong_channel = False
    Metrics.monetized = True
    channel = tmp_path / "channels/blow"
    write_token(channel)
    (tmp_path / "config.toml").write_text(CONFIG.split("[channels.science]")[0])
    published = (now - timedelta(days=6)).isoformat()
    (channel / "performance.json").write_text(
        json.dumps(
            [
                {
                    "observed_at": (now - timedelta(hours=24)).isoformat(),
                    "subscribers": 11100,
                    "videos": {
                        "owned-video": {
                            "views": 100,
                            "likes": 5,
                            "comments": 1,
                            "title": "Example",
                            "privacy": "public",
                            "publish_time": published,
                        }
                    },
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
                "publishedAt": published,
            },
            "status": {"privacyStatus": "public", "uploadStatus": "processed"},
            "contentDetails": {"duration": "PT10M"},
            "statistics": {"viewCount": "140", "likeCount": "7", "commentCount": "2"},
        }
    ]
    with provider(Metrics) as endpoint:
        Metrics.existing[0]["snippet"]["thumbnails"] = {
            "maxres": {"url": endpoint + "/thumbnail-max.jpg"},
            "high": {"url": endpoint + "/thumbnail-high.jpg"},
        }
        with app(tmp_path, FAKE_GOOGLE_URL=endpoint) as started:
            yield started


@pytest.mark.parametrize("monetized", [True, False])
def test_collect_report_and_tune_channel_with_delayed_revenue(metric_server, monetized):
    server, env = metric_server
    Metrics.monetized = monetized
    report = data(call(server, "channel_report", {"rank_by": "watch_hours"}))
    assert report["data_through"] > report["revenue_data_through"]
    assert report["daily"][0]["revenue_usd"] is None
    assert (
        data(call(server, "recent_performance"))["videos"][0]["counter_changes"]["24"][
            "views"
        ]
        == 40
    )
    # Private uploads often lack max-resolution artwork; the API rendition still previews.
    assert image(call(server, "view_thumbnail", {"video_id": "owned-video"})).size == (
        1920,
        1080,
    )
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
    result = subprocess.run(
        [sys.executable, "-c", collector],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    reading = data(call(server, "recent_performance"))["studio"]["videos"][0]
    assert reading["metrics"]["watch_hours"] == 354.4
    assert reading["metrics"]["ctr_percent"] == 6.04
    header = requests.get(
        server[0] + "/stats", headers={"Authorization": "Bearer " + TOKEN}, timeout=3
    ).text
    assert "9,401" in header and "-2" in header
    assert ("A$3.95" in header) == monetized
    assert "Earnings (AUD)" in header
    assert "$2.47" not in header
    path = server[1] / "channels/blow/studio-channel.json"
    snapshot = json.loads(path.read_text())
    assert snapshot["currency"] == "AUD"
    assert snapshot["daily"][max(snapshot["daily"])].get("revenue_aud") is None
    assert all("revenue_usd" not in values for values in snapshot["daily"].values())
    assert any(
        c["name"] == "SIDCC" and c["value"] == "rotated"
        for c in json.loads(session.read_text())
    )
    assert session.stat().st_mode & 0o777 == 0o600

    call(server, "update_video", {"video_id": "owned-video", "title": "A better title"})
    change = json.loads((server[1] / "channels/blow/experiments.json").read_text())[-1]
    assert (
        change["before"]["title"] == "Example"
        and change["after"]["title"] == "A better title"
    )
    assert change["baseline"]["counters"]["views"] == 140
    assert change["baseline"]["observed_at"] <= change["changed_at"]

    # A wrong channel must not overwrite the good readings, and old readings cannot count as fresh.
    Metrics.studio_wrong_channel = True
    wrong = subprocess.run(
        [sys.executable, "-c", collector],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert wrong.returncode == 1
    assert json.loads(path.read_text()) == snapshot
    snapshot["currency"] = "USD"
    path.write_text(json.dumps(snapshot))
    old_currency = requests.get(
        server[0] + "/stats", headers={"Authorization": "Bearer " + TOKEN}, timeout=3
    ).text
    assert "A$3.95" not in old_currency
    assert "fresh AUD collection" in old_currency
    snapshot["currency"] = "AUD"
    snapshot["observed_at"] = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    path.write_text(json.dumps(snapshot))
    stale = requests.get(
        server[0] + "/stats", headers={"Authorization": "Bearer " + TOKEN}, timeout=3
    ).text
    assert "9,401" not in stale
