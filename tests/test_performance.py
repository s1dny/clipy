"""Drive Analytics, counter snapshots and Studio import through real HTTP clients."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from test_mcp_server import CONFIG, ROOT, TOKEN, _free_port, call, data, image
from test_upload_recovery import LAUNCHER, YouTube


class Metrics(YouTube):
    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        path = urlsplit(self.path).path
        if path == "/thumbnail-max.jpg":
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


@pytest.fixture
def metric_server(tmp_path):
    now = datetime.now(UTC)
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
        deadline = time.monotonic() + 15
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
    assert f"Earnings (through {report['revenue_data_through']})" in response.text
    assert "Views: **42**" in response.text


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


def test_server_collector_reads_visible_cards_into_agent_report(metric_server):
    server, env = metric_server
    collector = (
        LAUNCHER.split("from clipy.mcp_server import main")[0]
        + """
from playwright.sync_api import sync_playwright
from clipy.channel import Channel
from clipy.studio_collect import collect
with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    context = browser.new_context()
    page = context.new_page()
    def studio_page(route):
        if '/channel/' in route.request.url:
            body = '<a aria-label="YouTube Studio dashboard" href="/channel/channel-owned">Studio</a>'
        else:
            overview = 'tab-overview' in route.request.url
            values = {'EXTERNAL_VIEWS': '9.4K', 'EXTERNAL_WATCH_TIME': '354.4', 'SUBSCRIBERS_NET_CHANGE': '+6', 'TOTAL_ESTIMATED_EARNINGS': '—'} if overview else {'VIDEO_THUMBNAIL_IMPRESSIONS': '34.5K', 'VIDEO_THUMBNAIL_IMPRESSIONS_VTR': '6.0%'}
            body = '<button>Oct 9 — Now Since published</button>' + ''.join(f'<div id="{key}-tab"><div id="metric-total">{value}</div></div>' for key,value in values.items())
        route.fulfill(content_type='text/html; charset=utf-8', body=body)
    page.context.route('https://studio.youtube.com/**', studio_page)
    print(json.dumps(collect(Channel('blow'), page)))
    assert page.context.pages == [page], 'Collector left video tabs running'
    browser.close()
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", collector],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved"] == 1, result.stdout
    report = data(call(server, "recent_performance"))
    reading = report["studio"]["videos"][0]
    assert reading["metrics"] == {
        "views": 9400,
        "watch_hours": 354.4,
        "subscribers": 6,
        "revenue_usd": None,
        "impressions": 34500,
        "ctr_percent": 6,
    }
    assert reading["approximate"] and reading["period"] == "since_published"


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
