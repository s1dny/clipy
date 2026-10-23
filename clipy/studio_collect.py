"""Read Studio analytics directly; Chromium is only used to recover a login."""

import copy
import fcntl
import hashlib
import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

from .analytics import channel_videos
from .channel import Channel, all_channels, data_root
from .common import read_json_file, write_json_file
from .studio import record

logger = logging.getLogger(__name__)
ORIGIN = "https://studio.youtube.com"
METRICS = {
    "EXTERNAL_VIEWS": ("views", 1),
    "EXTERNAL_WATCH_TIME": ("watch_hours", 3_600_000),
    "SUBSCRIBERS_NET_CHANGE": ("subscribers", 1),
    "TOTAL_ESTIMATED_EARNINGS": ("revenue_usd", 1_000),
    "VIDEO_THUMBNAIL_IMPRESSIONS": ("impressions", 1),
    "VIDEO_THUMBNAIL_IMPRESSIONS_VTR": ("ctr_percent", 1),
}


class StudioAuthRequired(RuntimeError):
    """Google needs a valid session or this account lacks channel access."""


def _session_path() -> Path | None:
    value = os.environ.get("CLIPY_STUDIO_SESSION")
    return Path(value) if value else None


def _allowed_cookie(cookie: dict) -> bool:
    domain = cookie["domain"].lstrip(".")
    return any(
        domain == root or domain.endswith("." + root)
        for root in ("google.com", "google.com.au", "google.nl", "youtube.com")
    )


class StudioClient:
    def __init__(self):
        self.session = requests.Session()
        self.configs = {}
        self.cookie_attributes = {}
        path = _session_path()
        if path and path.exists():
            self._set_cookies(json.loads(path.read_text()))
        # Browser metadata is cheap and contains no authentication data. Keep
        # its real user agent available when the browser is temporarily down.
        metadata = path.with_name("studio-client.json") if path else None
        if metadata and metadata.exists():
            self.session.headers["User-Agent"] = read_json_file(metadata)["user_agent"]
        try:
            endpoint = os.environ.get("CLIPY_STUDIO_CDP", "http://127.0.0.1:9222")
            agent = requests.get(endpoint + "/json/version", timeout=3).json()[
                "User-Agent"
            ]
            self.session.headers["User-Agent"] = agent
            if metadata:
                write_json_file(metadata, {"user_agent": agent})
        except requests.RequestException, ValueError, KeyError:
            pass

    def _set_cookies(self, cookies):
        for cookie in cookies:
            if not _allowed_cookie(cookie):
                continue
            expires = cookie.get("expires", -1)
            if expires != -1 and expires <= time.time():
                continue
            self.cookie_attributes[
                (cookie["domain"], cookie["path"], cookie["name"])
            ] = cookie
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
                path=cookie["path"],
                secure=cookie["secure"],
                expires=int(expires) if expires != -1 else None,
                rest={"HttpOnly": None} if cookie.get("httpOnly") else {},
            )

    def _recover_browser_login(self):
        # No pages, navigation, selectors, or renderer allocation. This only
        # reads the owner's current browser cookie jar after authentication fails.
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                os.environ.get("CLIPY_STUDIO_CDP", "http://127.0.0.1:9222"),
                timeout=10_000,
            )
            try:
                cookies = browser.contexts[0].cookies()
            finally:
                browser.close()
        self.session.cookies.clear()
        self._set_cookies(cookies)
        self.configs.clear()

    def _bootstrap(self, channel_id):
        response = self.session.get(f"{ORIGIN}/channel/{channel_id}", timeout=45)
        if (
            response.status_code in (401, 403)
            or urlsplit(response.url).hostname != urlsplit(ORIGIN).hostname
        ):
            raise StudioAuthRequired("Google session is signed out")
        response.raise_for_status()
        config = {}
        for match in re.finditer(r"ytcfg\.set\(", response.text):
            try:
                value, _ = json.JSONDecoder().raw_decode(response.text[match.end() :])
                if isinstance(value, dict):
                    config.update(value)
            except ValueError:
                continue
        if not config.get("INNERTUBE_API_KEY"):
            raise StudioAuthRequired("No authenticated Studio client context")
        if config.get("DELEGATION_CONTEXT", {}).get("externalChannelId") != channel_id:
            raise StudioAuthRequired("Studio account cannot access this channel")
        self.configs[channel_id] = config
        return config

    def request(self, channel_id: str, endpoint: str, body: dict) -> dict:
        """An authenticated Studio request; never retry a possibly applied write."""
        if not re.fullmatch(r"[a-z_]+/[a-z_]+", endpoint):
            raise ValueError("Use a Studio API resource/method endpoint.")
        if "context" in body:
            raise ValueError("Studio context is supplied by the channel session.")
        config = self.configs.get(channel_id) or self._bootstrap(channel_id)
        cookie = self.session.cookies.get("SAPISID", domain=".youtube.com", path="/")
        if not cookie:
            raise StudioAuthRequired("YouTube authentication cookie is missing")
        stamp = str(int(time.time()))
        digest = hashlib.sha1(f"{stamp} {cookie} {ORIGIN}".encode()).hexdigest()
        headers = {
            "Authorization": f"SAPISIDHASH {stamp}_{digest}",
            "Origin": ORIGIN,
            "X-Origin": ORIGIN,
            "X-Goog-Authuser": str(config.get("SESSION_INDEX", 0)),
            "X-Youtube-Client-Name": str(config["INNERTUBE_CONTEXT_CLIENT_NAME"]),
            "X-Youtube-Client-Version": config["INNERTUBE_CONTEXT_CLIENT_VERSION"],
        }
        if config.get("INNERTUBE_CONTEXT_SERIALIZED_DELEGATION_CONTEXT"):
            headers["X-Youtube-Delegation-Context"] = config[
                "INNERTUBE_CONTEXT_SERIALIZED_DELEGATION_CONTEXT"
            ]
        context = copy.deepcopy(config["INNERTUBE_CONTEXT"])
        context.setdefault("user", {})["delegationContext"] = config[
            "DELEGATION_CONTEXT"
        ]
        if config.get("INNERTUBE_CONTEXT_SERIALIZED_DELEGATION_CONTEXT"):
            context["user"]["serializedDelegationContext"] = config[
                "INNERTUBE_CONTEXT_SERIALIZED_DELEGATION_CONTEXT"
            ]
        response = self.session.post(
            ORIGIN + "/youtubei/v1/" + endpoint,
            params={"key": config["INNERTUBE_API_KEY"], "alt": "json"},
            headers=headers,
            json={**body, "context": context},
            timeout=45,
        )
        if response.status_code in (401, 403):
            raise StudioAuthRequired("Studio rejected authentication")
        response.raise_for_status()
        payload = response.json()
        if (
            payload.get("responseContext", {})
            .get("webResponseContextExtensionData", {})
            .get("challenge")
        ):
            raise StudioAuthRequired(
                "Studio requires an authentication challenge; the write is unconfirmed. Recheck the remote setting before retrying."
            )
        if "error" in payload:
            raise ValueError("Studio request failed: " + str(payload["error"]))
        return payload

    def _cards(
        self, channel_id, video_id, currency, start=None, end=None, include_revenue=True
    ):
        entity = {"videoId": video_id} if video_id else {"channelId": channel_id}
        period = (
            {
                "dateIdRange": {
                    "inclusiveStart": int(start.strftime("%Y%m%d")),
                    "exclusiveEnd": int(end.strftime("%Y%m%d")),
                }
            }
            if start is not None and end is not None
            else {
                "referencePoint": "TIME_PERIOD_REFERENCE_POINT_SINCE_PUBLISH",
                "timePeriodType": "ANALYTICS_TIME_PERIOD_TYPE_SINCE_PUBLISH",
                "entity": entity,
            }
        )
        query = {
            "screenConfig": {
                "entity": entity,
                "timePeriod": period,
                "currency": currency,
                "timeZoneOffsetSecs": int(
                    datetime.now(ZoneInfo("Australia/Sydney"))
                    .utcoffset()
                    .total_seconds()
                ),
            },
            "cardConfigs": [
                {
                    "autoUpdateInterval": "ANALYTICS_AUTO_UPDATE_INTERVAL_NEVER",
                    "keyMetricCardConfig": {
                        "timePeriod": {},
                        "metricTabConfigs": [
                            {"metric": metric}
                            for metric in METRICS
                            if include_revenue or metric != "TOTAL_ESTIMATED_EARNINGS"
                        ],
                    },
                    "failureMode": "ANALYTICS_CARD_FAILURE_MODE_FAIL_PAGE",
                    "disableSeeMoreButton": True,
                    "topEntitySelection": {"channelId": channel_id},
                }
            ],
            "fetchingType": "FETCHING_TYPE_FOREGROUND",
        }
        payload = self.request(channel_id, "yta_web/get_cards", query)
        values = {}
        daily = {}
        estimated = False
        for card in payload.get("cards", []):
            if (
                card.get("config", {}).get("topEntitySelection", {}).get("channelId")
                != channel_id
            ):
                raise ValueError("Studio response does not match the requested channel")
            for tab in card.get("keyMetricCardData", {}).get("keyMetricTabs", []):
                content = tab.get("primaryContent", {})
                metric = content.get("metric")
                if metric in METRICS:
                    name, divisor = METRICS[metric]
                    if metric == "TOTAL_ESTIMATED_EARNINGS":
                        name = f"revenue_{currency.lower()}"
                    total = content.get("total")
                    values[name] = total / divisor if total is not None else None
                    anomalies = content.get("anomalies", [])
                    estimated |= bool(anomalies)
                    if start is not None:
                        for datum in content.get("mainSeries", {}).get("datums", []):
                            stamp = datum["x"]
                            day = (
                                datetime.fromtimestamp(
                                    stamp / 1000, ZoneInfo("America/Los_Angeles")
                                )
                                .date()
                                .isoformat()
                            )
                            missing = any(
                                anomaly.get("impact")
                                == "ANALYTICS_ANOMALY_IMPACT_DATA_MISSING"
                                and anomaly.get("startDate", float("-inf"))
                                <= stamp
                                < anomaly.get("endDate", float("inf"))
                                for anomaly in anomalies
                            )
                            value = datum.get("y")
                            daily.setdefault(day, {})[name] = (
                                value / divisor
                                if value is not None and not missing
                                else None
                            )
        if start is not None:
            if not daily:
                raise ValueError("Studio returned no channel daily series")
            return daily, estimated
        if not values:
            raise ValueError("Studio returned no metric totals")
        return {name: values.get(name) for name, _ in METRICS.values()}, estimated

    def readings(
        self,
        channel_id,
        video_id,
        currency="USD",
        *,
        start=None,
        end=None,
        include_revenue=True,
    ):
        try:
            return self._cards(
                channel_id, video_id, currency, start, end, include_revenue
            )
        except StudioAuthRequired:
            # Refresh client variables and cookie state once. A revoked Google
            # login is reported, never disguised as a zero-valued reading.
            self.configs.pop(channel_id, None)
            try:
                return self._cards(
                    channel_id, video_id, currency, start, end, include_revenue
                )
            except StudioAuthRequired:
                self._recover_browser_login()
                return self._cards(
                    channel_id, video_id, currency, start, end, include_revenue
                )

    def checkpoint(self):
        path = _session_path()
        if path is None:
            return
        cookies = []
        for cookie in self.session.cookies:
            identity = (cookie.domain, cookie.path, cookie.name)
            value = {
                **self.cookie_attributes.get(identity, {}),
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "httpOnly": "HttpOnly" in cookie._rest,
                "expires": cookie.expires if cookie.expires is not None else -1,
            }
            if _allowed_cookie(value):
                cookies.append(value)
        # Called only after an authenticated, channel-checked analytics response.
        if any(cookie["name"] == "SID" for cookie in cookies):
            write_json_file(path, cookies)

    def close(self):
        self.session.close()


def collect(channel: Channel, client: StudioClient) -> dict:
    account = (
        channel.youtube()
        .channels()
        .list(part="snippet,status", mine=True)
        .execute()["items"][0]
    )
    channel_id = account["id"]
    monetized = account.get("status", {}).get("isChannelMonetizationEnabled", False)
    public = [
        v for v in channel_videos(channel, refresh=True) if v["privacy"] == "public"
    ]
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
        targets = [
            v for v in dict.fromkeys([*reversed(active), *targets]) if v in known
        ][:10]
    saved, errors = 0, []
    try:
        end = datetime.now(ZoneInfo("Australia/Sydney")).date()
        start = end - timedelta(days=70)
        daily, approximate = client.readings(
            channel_id,
            None,
            currency="AUD",
            start=start,
            end=end,
            include_revenue=monetized,
        )
        write_json_file(
            channel.dir / "studio-channel.json",
            {
                "channel_id": channel_id,
                "monetized": monetized,
                "observed_at": datetime.now(UTC).isoformat(),
                "source": "YouTube Studio internal analytics API",
                "currency": "AUD",
                "reporting_timezone": "America/Los_Angeles",
                "start_date": start.isoformat(),
                "end_date_exclusive": end.isoformat(),
                "approximate": approximate,
                "daily": daily,
            },
        )
        client.checkpoint()
    except Exception as exc:  # noqa: BLE001 - video collection can still proceed
        logger.warning(
            "Studio channel totals unavailable for %s: %s",
            channel.name,
            type(exc).__name__,
        )
        error = {"scope": "channel_totals", "error": type(exc).__name__}
        if isinstance(exc, StudioAuthRequired):
            error["action"] = (
                "Sign into the server browser; Google requires authentication or channel access."
            )
        errors.append(error)
    for video_id in targets:
        try:
            metrics, approximate = client.readings(channel_id, video_id)
            record(
                channel,
                [
                    {
                        "video_id": video_id,
                        "observed_at": datetime.now(UTC).isoformat(),
                        "url": f"{ORIGIN}/video/{video_id}/analytics/tab-overview/period-default",
                        "period": "since_published",
                        "approximate": approximate,
                        "currency": "USD",
                        "metrics": metrics,
                    }
                ],
                source="YouTube Studio internal analytics API",
            )
            saved += 1
            client.checkpoint()
        except Exception as exc:  # noqa: BLE001 - retain prior readings when one video is unavailable
            logger.warning(
                "Studio API unavailable for %s/%s: %s",
                channel.name,
                video_id,
                type(exc).__name__,
            )
            error = {"video_id": video_id, "error": type(exc).__name__}
            if isinstance(exc, StudioAuthRequired):
                error["action"] = (
                    "Sign into the server browser; Google requires authentication or channel access."
                )
            errors.append(error)
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "saved": saved,
        "errors": errors,
        "method": "direct_api",
    }


def run(channel_name: str | None = None) -> int:
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
    client = StudioClient()
    failed = False
    try:
        for channel in [Channel(channel_name)] if channel_name else all_channels():
            try:
                status = collect(channel, client)
            except Exception as exc:  # noqa: BLE001 - one channel must not discard the other channels' readings
                logger.warning(
                    "Studio API unavailable for %s: %s",
                    channel.name,
                    type(exc).__name__,
                )
                status = {
                    "checked_at": datetime.now(UTC).isoformat(),
                    "saved": 0,
                    "errors": [{"error": type(exc).__name__}],
                    "method": "direct_api",
                }
            write_json_file(channel.dir / "studio-status.json", status)
            failed |= bool(status["errors"])
    finally:
        client.close()
    return int(failed)
