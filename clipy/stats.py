"""Daily status totals from fresh channel-level Studio readings."""

import logging
from datetime import UTC, date, datetime, timedelta

from .channel import Channel, all_channels
from .common import read_json_file
from .upload import YouTubeAuthError

logger = logging.getLogger(__name__)

WINDOW = 30


def _daily(channel: Channel) -> dict[str, dict]:
    if not channel.token_file.exists():
        raise YouTubeAuthError("Channel needs YouTube authorization")
    path = channel.dir / "studio-channel.json"
    if not path.exists():
        raise ValueError("channel totals not collected yet")
    reading = read_json_file(path)
    observed = datetime.fromisoformat(reading["observed_at"])
    if observed.tzinfo is None or not timedelta(0) <= datetime.now(
        UTC
    ) - observed <= timedelta(hours=2):
        raise ValueError("Studio channel totals are over two hours old")
    if reading.get("currency") != "AUD":
        raise ValueError("Studio totals need a fresh AUD collection")
    return {
        day: {
            "views": metrics.get("views"),
            "subscribers": metrics.get("subscribers"),
            "earnings": metrics.get("revenue_aud"),
        }
        for day, metrics in reading["daily"].items()
    }


def _total(daily: dict[str, dict], metric: str, first: date, last: date) -> float:
    return sum(
        (values.get(metric) or 0)
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
    ("earnings", "Earnings (AUD)", lambda n: f"A${n:,.2f}", lambda n: f"A${n:,.2f}"),
    ("views", "Views", lambda n: f"{round(n):,}", _compact),
    (
        "subscribers",
        "Subscribers",
        lambda n: f"{round(n):+,}",
        lambda n: f"{round(n):+,}",
    ),
)


def report() -> str:
    channels = all_channels()
    names, daily, unavailable = {}, {}, {}
    for channel in channels:
        names[channel.name] = channel.config().channel.handle
        try:
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
                str(exc)
                if isinstance(exc, ValueError)
                else f"Studio totals unavailable ({type(exc).__name__})"
            )
    # Studio supplies recent engagement; processing revenue stays unavailable.
    latest = [max(d) for d in daily.values() if d]
    if not latest:
        return "Analytics unavailable.\n" + "\n".join(
            f"• {names[name]}: {reason}" for name, reason in unavailable.items()
        )
    day = date.fromisoformat(min(latest))
    label = f"{day:%a} {day.day} {day:%b}"

    lines = [label + "  ", ""]
    if unavailable:
        lines += ["Totals include available channels only.  ", ""]
    for metric, heading, one_day, window in SECTIONS:
        metric_days = [
            max(d for d, values in data.items() if values.get(metric) is not None)
            for data in daily.values()
            if any(values.get(metric) is not None for values in data.values())
        ]
        if not metric_days:
            lines.append(f"{heading}: unavailable  ")
            continue
        metric_day = date.fromisoformat(min(metric_days))
        metric_window = (metric_day - timedelta(days=WINDOW - 1), metric_day)
        rows = {
            name: (
                daily[name].get(metric_day.isoformat(), {}).get(metric),
                _total(daily[name], metric, *metric_window),
            )
            for name in daily
        }
        on_day, total = (
            sum(r[i] for r in rows.values() if r[i] is not None) for i in range(2)
        )
        lines.append(
            f"{heading}: **{one_day(on_day)}** "
            f"(Last {WINDOW} days: **{window(total)}**)  "
        )
        for channel in channels:
            name = channel.name
            if name in unavailable:
                lines.append(f"• {names[name]}: unavailable ({unavailable[name]})  ")
            else:
                value = rows[name][0]
                unavailable_label = "pending / unavailable"
                if (
                    metric == "earnings"
                    and read_json_file(channel.dir / "studio-channel.json").get(
                        "monetized"
                    )
                    is False
                ):
                    unavailable_label = "not monetized"
                lines.append(
                    f"• {names[name]}: **{one_day(value)}**  "
                    if value is not None
                    else f"• {names[name]}: {unavailable_label}  "
                )
        lines.append("")
    return "\n".join(lines).rstrip()
