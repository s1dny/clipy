"""Each channel's daily report to the owner over Matrix.

The agent sends the report with the send_report tool; when the run ends,
agent/run.sh edits that message to append the run's stats line, so each
channel's day is one message:

    python -m clipy.matrix finish CHANNEL STARTED_EPOCH STATS [FAILURE_FILE]

The bot opens a direct room with MATRIX_NOTIFY_USER on first use, shared by
every channel, and keeps its id in the data directory; each channel keeps its
last report's in its own directory.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import markdown

from .channel import Channel, data_root

ROOM_FILE = data_root() / "matrix-room"


def configured() -> bool:
    return bool(os.environ.get("MATRIX_ACCESS_TOKEN"))


def _call(method: str, path: str, body: dict) -> dict:
    homeserver = os.environ.get("MATRIX_HOMESERVER", "http://tuwunel.tuwunel.svc:8008")
    request = urllib.request.Request(
        homeserver + path,
        data=json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['MATRIX_ACCESS_TOKEN']}",
            "Content-Type": "application/json",
            "User-Agent": "clipy",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _room() -> str:
    if ROOM_FILE.exists():
        return ROOM_FILE.read_text().strip()
    room_id = _call(
        "POST",
        "/_matrix/client/v3/createRoom",
        {
            "preset": "trusted_private_chat",
            "is_direct": True,
            "invite": [os.environ["MATRIX_NOTIFY_USER"]],
            "name": "clipy status",
            "topic": "Daily reports from clipy",
        },
    )["room_id"]
    ROOM_FILE.write_text(room_id)
    return room_id


def _content(text: str) -> dict:
    return {
        "msgtype": "m.text",
        "body": text,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown.markdown(text, extensions=["sane_lists"]),
    }


def _put(room: str, content: dict) -> str:
    path = (
        f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/send/m.room.message/"
        f"clipy-{time.time_ns()}"
    )
    return _call("PUT", path, content)["event_id"]


def send(channel: Channel, text: str) -> str:
    """Post a Markdown report headed with the channel's handle, and remember it
    as the channel's latest."""
    text = f"## {channel.config().channel.handle}\n\n{text}"
    room = _room()
    event_id = _put(room, _content(text))
    channel.last_report_file.write_text(
        json.dumps(
            {"room": room, "event_id": event_id, "text": text, "at": time.time()}
        )
    )
    return event_id


def finish(
    channel: Channel, started: float, stats: str, failure: str | None = None
) -> None:
    """Append the stats line to this run's report, or report the failure."""
    path = channel.last_report_file
    last = json.loads(path.read_text()) if path.exists() else {}
    if failure is None and last.get("at", 0) >= started:
        text = f"{last['text']}\n\n{stats}"
        content = _content(text)
        _put(
            last["room"],
            {
                **_content(f"* {text}"),
                "m.new_content": content,
                "m.relates_to": {"rel_type": "m.replace", "event_id": last["event_id"]},
            },
        )
        return
    reason = failure or "clipy finished without sending a report."
    _put(_room(), _content(f"## {channel.name}\n\n{reason}\n\n{stats}"))


if __name__ == "__main__":
    command, name, started, stats, *rest = sys.argv[1:]
    assert command == "finish"
    failure = Path(rest[0]).read_text() if rest else None
    finish(Channel(name), float(started), stats, failure)
