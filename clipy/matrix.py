"""The daily report to the owner over Matrix: one message covering every
channel, which agent/run.sh assembles and sends when the day's runs end:

    python -m clipy.matrix send REPORT_FILE

The bot opens a direct room with MATRIX_NOTIFY_USER on first use and keeps
its id in the data directory.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import markdown

from .channel import data_root

ROOM_FILE = data_root() / "matrix-room"


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
    """Post a Markdown message; returns its event id."""
    return _put(_room(), _content(text))


if __name__ == "__main__":
    command, path = sys.argv[1:]
    assert command == "send"
    print(send(Path(path).read_text(encoding="utf-8")))
