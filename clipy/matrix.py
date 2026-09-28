"""The daily report to the channel owner over Matrix.

The agent sends the report with the send_report tool; when the run ends,
agent/run.sh edits that message to append the run's stats line, so each day
is one message:

    python -m clipy.matrix finish STARTED_EPOCH STATS [FAILURE_FILE]

The bot opens a direct room with MATRIX_NOTIFY_USER on first use and keeps
its id, and the last report's, in the data directory.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import markdown

DATA_DIR = Path(os.environ.get("CLIPY_DATA_DIR", "/data"))
ROOM_FILE = DATA_DIR / "matrix-room"
LAST_REPORT_FILE = DATA_DIR / "last-report.json"


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


def send(text: str) -> str:
    """Post a Markdown message and remember it as the latest report."""
    room = _room()
    event_id = _put(room, _content(text))
    LAST_REPORT_FILE.write_text(
        json.dumps(
            {"room": room, "event_id": event_id, "text": text, "at": time.time()}
        )
    )
    return event_id


def finish(started: float, stats: str, failure: str | None = None) -> None:
    """Append the stats line to this run's report, or report the failure."""
    last = json.loads(LAST_REPORT_FILE.read_text()) if LAST_REPORT_FILE.exists() else {}
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
    _put(_room(), _content(f"{reason}\n\n{stats}"))


if __name__ == "__main__":
    command, started, stats, *rest = sys.argv[1:]
    assert command == "finish"
    failure = Path(rest[0]).read_text() if rest else None
    finish(float(started), stats, failure)
