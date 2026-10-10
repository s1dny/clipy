"""Read visible Studio metric cards from the dedicated server browser."""

import fcntl
import logging
import os
import re
from datetime import UTC, datetime, timedelta

from playwright.sync_api import sync_playwright

from .analytics import channel_videos
from .channel import Channel, all_channels, data_root
from .common import read_json_file, write_json_file
from .studio import record

logger = logging.getLogger(__name__)
CARDS = {
    "overview": {
        "EXTERNAL_VIEWS": "views",
        "EXTERNAL_WATCH_TIME": "watch_hours",
        "SUBSCRIBERS_NET_CHANGE": "subscribers",
        "TOTAL_ESTIMATED_EARNINGS": "revenue_usd",
    },
    "reach_viewers": {
        "VIDEO_THUMBNAIL_IMPRESSIONS": "impressions",
        "VIDEO_THUMBNAIL_IMPRESSIONS_VTR": "ctr_percent",
    },
}


def _number(text: str) -> float | None:
    text = (
        text.strip()
        .replace(",", "")
        .replace("\u2212", "-")
        .replace("$", "")
        .replace("%", "")
    )
    if text in {"", "—", "–", "-"}:
        return None
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?[KMB]?", text):
        raise ValueError(f"Unrecognised Studio metric value: {text!r}")
    multiplier = {"K": 1000, "M": 1_000_000, "B": 1_000_000_000}.get(text[-1], 1)
    return float(text[:-1] if multiplier != 1 else text) * multiplier


def collect(channel: Channel, page) -> dict:
    mine = (
        channel.youtube()
        .channels()
        .list(part="snippet", mine=True)
        .execute()["items"][0]
    )
    channel_id = mine["id"]
    page.goto(
        f"https://studio.youtube.com/channel/{channel_id}",
        wait_until="domcontentloaded",
    )
    page.get_by_role("link", name="YouTube Studio dashboard", exact=True).wait_for(
        timeout=60_000
    )
    href = page.get_by_role(
        "link", name="YouTube Studio dashboard", exact=True
    ).get_attribute("href")
    if f"/channel/{channel_id}" not in (href or ""):
        raise ValueError(
            f"Studio browser is not signed into {channel.config().channel.handle}; switch/sign into this channel once."
        )
    videos = channel_videos(channel, refresh=True)
    public = [v for v in videos if v["privacy"] == "public"]
    targets = [v["id"] for v in public[:5]]
    path = channel.dir / "experiments.json"
    if path.exists():
        active = [
            e["video_id"]
            for e in read_json_file(path)
            if datetime.now(UTC) - datetime.fromisoformat(e["changed_at"])
            < timedelta(days=14)
        ]
        known = {v["id"] for v in public}
        targets = list(dict.fromkeys([*reversed(active), *targets]))
        targets = [v for v in targets if v in known][:10]
    # Studio retains substantial renderer memory across SPA navigations. Keep
    # the ownership-check tab idle and release each video's renderer afterward.
    context = page.context
    page.goto("about:blank")
    saved, errors = 0, []
    for video_id in targets:
        video_page = None
        try:
            metrics = {}
            for tab, cards in CARDS.items():
                if video_page is not None:
                    video_page.close()
                video_page = context.new_page()
                url = f"https://studio.youtube.com/video/{video_id}/analytics/tab-{tab}/period-default"
                video_page.goto(url, wait_until="domcontentloaded")
                first_card = next(iter(cards))
                video_page.locator(f"#{first_card}-tab #metric-total").wait_for(
                    timeout=60_000
                )
                if not video_page.get_by_role(
                    "button", name=re.compile("Since published")
                ).first.is_visible():
                    raise ValueError(
                        "Studio is not displaying Since published; retain prior readings."
                    )
                for card, metric in cards.items():
                    node = video_page.locator(f"#{card}-tab #metric-total")
                    if node.count():
                        metrics[metric] = _number(node.inner_text())
            record(
                channel,
                [
                    {
                        "video_id": video_id,
                        "observed_at": datetime.now(UTC).isoformat(),
                        "url": url,
                        "period": "since_published",
                        "approximate": True,
                        "metrics": metrics,
                    }
                ],
            )
            saved += 1
        except Exception as exc:  # noqa: BLE001 - one unavailable reading must not discard the others
            logger.warning(
                "Studio reading failed for %s/%s: %s",
                channel.name,
                video_id,
                type(exc).__name__,
            )
            errors.append({"video_id": video_id, "error": type(exc).__name__})
        finally:
            if video_page is not None and not video_page.is_closed():
                video_page.close()
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "saved": saved,
        "errors": errors,
    }


def run(channel_name: str | None = None) -> int:
    # Scheduled and manual collection share the same browser and memory budget.
    # Keep the descriptor open for the whole run; the kernel releases it on exit.
    path = data_root() / "studio-collector.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Studio collection already running; skipping overlap.")
            return 0
        return _run(channel_name)


def _run(channel_name: str | None = None) -> int:
    channels = [Channel(channel_name)] if channel_name else all_channels()
    failed = False
    with sync_playwright() as playwright:
        # This endpoint is localhost inside the browser pod, never a public CDP.
        browser = playwright.chromium.connect_over_cdp(
            os.environ.get("CLIPY_STUDIO_CDP", "http://127.0.0.1:9222"), timeout=30_000
        )
        context = browser.contexts[0]
        # This dedicated browser is also used for the owner's one-time login.
        # Keep those completed Studio tabs idle before allocating a collector
        # renderer; never navigate an in-progress Google sign-in page.
        for existing in context.pages:
            if existing.url.startswith("https://studio.youtube.com/"):
                existing.goto("about:blank")
        page = context.new_page()
        try:
            for channel in channels:
                try:
                    status = collect(channel, page)
                except Exception as exc:  # noqa: BLE001 - one unavailable reading must not discard the others
                    status = {
                        "checked_at": datetime.now(UTC).isoformat(),
                        "saved": 0,
                        "errors": [
                            {
                                "error": type(exc).__name__,
                                "action": "Open the server browser and sign into this channel; verify Studio is available.",
                            }
                        ],
                    }
                    logger.warning(
                        "Studio unavailable for %s: %s",
                        channel.name,
                        type(exc).__name__,
                    )
                write_json_file(channel.dir / "studio-status.json", status)
                failed |= bool(status["errors"])
        finally:
            page.close()
            # Disconnect, leaving the persistent signed-in Chromium running.
            browser.close()
    return int(failed)
