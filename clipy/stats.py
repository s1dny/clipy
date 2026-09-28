"""The stats at the top of the daily report: earnings, views and subscribers
for every channel, on the newest day YouTube has data for and over the 30
days up to it, against the 30 days before."""

from datetime import UTC, date, datetime, timedelta

from .analytics import _query
from .channel import Channel, all_channels

WINDOW = 30


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


def _change(now: float, before: float) -> str:
    if not before:
        return "🟩 **new**" if now > 0 else "⬜ **0.0%**"
    percent = (now - before) / abs(before) * 100
    square = "🟩" if percent > 0 else "🟥" if percent < 0 else "⬜"
    return f"{square} **{percent:+.1f}%**"


def _compact(n: float) -> str:
    n = round(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 100_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:,}"


SECTIONS = (
    ("earnings", "💰 **Earnings**", lambda n: f"${n:,.2f}", lambda n: f"${n:,.2f}"),
    ("views", "👁️ **Views**", lambda n: f"{round(n):,}", _compact),
    (
        "subscribers",
        "👥 **Subscribers**",
        lambda n: f"{round(n):+,}",
        lambda n: f"{round(n):+,}",
    ),
)


def report() -> str:
    channels = all_channels()
    names, daily = {}, {}
    for channel in channels:
        mine = channel.youtube().channels().list(part="snippet", mine=True).execute()
        names[channel.name] = mine["items"][0]["snippet"]["title"]
        daily[channel.name] = _daily(channel)
    # The newest day every channel with data has reached (about 3 days ago).
    latest = [max(d) for d in daily.values() if d]
    if not latest:
        return "No analytics yet."
    day = date.fromisoformat(min(latest))
    now = (day - timedelta(days=WINDOW - 1), day)
    before = (now[0] - timedelta(days=WINDOW), now[0] - timedelta(days=1))
    label = f"{day:%a} {day.day} {day:%b}"

    lines = []
    for metric, heading, one_day, window in SECTIONS:
        rows = {
            name: (
                daily[name].get(day.isoformat(), {}).get(metric, 0),
                _total(daily[name], metric, *now),
                _total(daily[name], metric, *before),
            )
            for name in daily
        }
        on_day, total, previous = (sum(r[i] for r in rows.values()) for i in range(3))
        lines += [
            heading,
            f"**{label}:** {one_day(on_day)}  ",
            f"**Last {WINDOW} days:** {window(total)} — {_change(total, previous)}",
            "",
        ]
        lines += [
            f"• **{names[name]}:** {one_day(d)} on {day.day} {day:%b} · "
            f"{window(t)} last {WINDOW}d — {_change(t, p)}  "
            for name, (d, t, p) in rows.items()
        ]
        lines.append("")
    return "\n".join(lines).rstrip()
