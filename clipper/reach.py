"""Thumbnail impressions and click-through from the YouTube Reporting API.

The Analytics API has no impressions or CTR; the Reporting API's daily
channel_reach_basic_a1 reports do. YouTube writes one CSV per day (plus 30
days of history when the job is created), about two days behind. They are
downloaded into reach.json as {"YYYY-MM-DD|video_id": [impressions, ctr]}.
"""

import csv
import io
import logging
import threading
import time
from datetime import date
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build

from .common import read_json_file, write_json_file
from .upload import get_credentials

logger = logging.getLogger(__name__)

REPORT_TYPE = "channel_reach_basic_a1"
SYNC_EVERY_SECONDS = 3 * 3600

_lock = threading.Lock()
_last_sync = 0.0


def _load(store: Path) -> dict:
    if store.exists():
        return read_json_file(store)
    return {"reports": [], "rows": {}}


def sync(token: Path, store: Path, force: bool = False) -> dict:
    """Download reach reports not seen yet (at most every few hours) and
    return the stored rows."""
    global _last_sync
    with _lock:
        data = _load(store)
        if not force and time.monotonic() - _last_sync < SYNC_EVERY_SECONDS:
            return data
        credentials = get_credentials(token)
        api = build(
            "youtubereporting", "v1", credentials=credentials, cache_discovery=False
        )
        jobs = api.jobs().list().execute().get("jobs", [])
        job = next((j for j in jobs if j["reportTypeId"] == REPORT_TYPE), None)
        if job is None:
            job = (
                api.jobs()
                .create(body={"reportTypeId": REPORT_TYPE, "name": "clipper reach"})
                .execute()
            )
            logger.info("Created reach reporting job %s", job["id"])

        reports, page = [], None
        while True:
            resp = api.jobs().reports().list(jobId=job["id"], pageToken=page).execute()
            reports += resp.get("reports", [])
            page = resp.get("nextPageToken")
            if not page:
                break

        session = AuthorizedSession(credentials)
        done = set(data["reports"])
        for report in sorted(reports, key=lambda r: r["createTime"]):
            if report["id"] in done:
                continue
            resp = session.get(report["downloadUrl"], timeout=120)
            resp.raise_for_status()
            for row in csv.DictReader(io.StringIO(resp.text)):
                day = date(
                    int(row["date"][:4]), int(row["date"][4:6]), int(row["date"][6:])
                )
                data["rows"][f"{day.isoformat()}|{row['video_id']}"] = [
                    int(row["video_thumbnail_impressions"]),
                    float(row["video_thumbnail_impressions_ctr"]),
                ]
            data["reports"].append(report["id"])
        write_json_file(store, data)
        _last_sync = time.monotonic()
        return data


def totals(data: dict, start: date, end: date) -> dict[str, dict]:
    """Per video over [start, end]: impressions and click-through (percent)."""
    sums: dict[str, list[float]] = {}
    for key, (impressions, ctr) in data["rows"].items():
        day, video = key.split("|")
        if start.isoformat() <= day <= end.isoformat():
            acc = sums.setdefault(video, [0, 0.0])
            acc[0] += impressions
            acc[1] += impressions * ctr
    return {
        video: {
            "impressions": int(impressions),
            "ctr_percent": round(100 * clicks / impressions, 2)
            if impressions
            else None,
        }
        for video, (impressions, clicks) in sums.items()
    }


def daily(data: dict, video_id: str, start: date) -> list[dict]:
    return [
        {"day": day, "impressions": impressions, "ctr_percent": round(100 * ctr, 2)}
        for key, (impressions, ctr) in sorted(data["rows"].items())
        for day, video in [key.split("|")]
        if video == video_id and day >= start.isoformat()
    ]
