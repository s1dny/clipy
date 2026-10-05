"""The stats at the top of the daily report: earnings, views and subscribers
for every channel, on the newest day YouTube has data for and over the 30
days up to it."""

import logging
from datetime import UTC, date, datetime, timedelta

from .analytics import _query
from .channel import Channel, all_channels
from .upload import YouTubeAuthError

logger = logging.getLogger(__name__)

WINDOW = 30
CHANNEL_LABELS = {"blow": "JBlow", "science": "Science", "philosophy": "Philosophy"}


def _daily(channel: Channel) -> dict[str, dict]:
    end = datetime.now(UTC).date()
    rows = _query(
        channel,
        metrics="views,estimatedRevenue,subscribersGained,subscribersLost",
        dimensions="day",
        sort="day",
        startDate=(end - timedelta(days=2 * WINDOW + 10)).isoformat(),
        endDate=end.isoformat(),
    )
    return {
        row["day"]: {
            "earnings": row["estimatedRevenue"],
            "views": row["views"],
            "subscribers": row["subscribersGained"] - row["subscribersLost"],
        }
        for row in rows
    }


def _total(daily: dict[str, dict], metric: str, first: date, last: date) -> float:
    return sum(
        values[metric]
        for day, values in daily.items()
        if first.isoformat() <= day <= last.isoformat()
    )


def _compact(n: float) -> str:
    n = round(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 100_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:,}"


SECTIONS = (
    ("earnings", "Earnings", lambda n: f"${n:,.2f}", lambda n: f"${n:,.2f}"),
    ("views", "Views", lambda n: f"{round(n):,}", _compact),
    (
        "subscribers",
        "Subscribers",
        lambda n: f"{round(n):+,}",
        lambda n: f"{round(n):+,}",
    ),
)


def report() -> str:
    order = {name: index for index, name in enumerate(CHANNEL_LABELS)}
    channels = sorted(
        all_channels(), key=lambda channel: order.get(channel.name, len(order))
    )
    names, daily, unavailable = {}, {}, {}
    for channel in channels:
        names[channel.name] = CHANNEL_LABELS.get(channel.name, channel.name)
        try:
            if channel.name not in CHANNEL_LABELS:
                mine = (
                    channel.youtube()
                    .channels()
                    .list(part="snippet", mine=True)
                    .execute()
                )
                names[channel.name] = mine["items"][0]["snippet"]["title"]
            data = _daily(channel)
            if data:
                daily[channel.name] = data
            else:
                unavailable[channel.name] = "no analytics yet"
        except YouTubeAuthError:
            logger.exception("YouTube sign-in unavailable for %s", channel.name)
            unavailable[channel.name] = (
                f"reconnect YouTube with clipy auth {channel.name}"
            )
        except Exception as exc:
            logger.exception("Analytics unavailable for %s", channel.name)
            unavailable[channel.name] = (
                f"analytics request failed ({type(exc).__name__})"
            )
    # The newest day every channel with data has reached (about 3 days ago).
    latest = [max(d) for d in daily.values() if d]
    if not latest:
        return "Analytics unavailable.\n" + "\n".join(
            f"• {names[name]}: {reason}" for name, reason in unavailable.items()
        )
    day = date.fromisoformat(min(latest))
    now = (day - timedelta(days=WINDOW - 1), day)
    label = f"{day:%a} {day.day} {day:%b}"

    lines = [label + "  "]
    if unavailable:
        lines += ["Totals include available channels only.  ", ""]
    for metric, heading, one_day, window in SECTIONS:
        rows = {
            name: (
                daily[name].get(day.isoformat(), {}).get(metric, 0),
                _total(daily[name], metric, *now),
            )
            for name in daily
        }
        on_day, total = (sum(r[i] for r in rows.values()) for i in range(2))
        lines.append(
            f"{heading}: **{one_day(on_day)}** "
            f"(Last {WINDOW} days: **{window(total)}**)  "
        )
        for channel in channels:
            name = channel.name
            if name in unavailable:
                lines.append(f"• {names[name]}: unavailable ({unavailable[name]})  ")
            else:
                lines.append(f"• {names[name]}: **{one_day(rows[name][0])}**  ")
        lines.append("")
    return "\n".join(lines).rstrip()
