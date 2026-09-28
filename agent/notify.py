"""Post a message to Aiden over Matrix: `python notify.py <file>` sends the
file's text. The bot opens a direct room with MATRIX_NOTIFY_USER on first use
and remembers it in MATRIX_ROOM_FILE."""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOMESERVER = os.environ.get("MATRIX_HOMESERVER", "http://tuwunel.tuwunel.svc:8008")
TOKEN = os.environ["MATRIX_ACCESS_TOKEN"]
RECIPIENT = os.environ.get("MATRIX_NOTIFY_USER", "@aiden:matrix.aza.network")
ROOM_FILE = Path(os.environ.get("MATRIX_ROOM_FILE", "/data/matrix-room"))


def call(method: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        HOMESERVER + path,
        data=json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def room() -> str:
    if ROOM_FILE.exists():
        return ROOM_FILE.read_text().strip()
    room_id = call(
        "POST",
        "/_matrix/client/v3/createRoom",
        {
            "preset": "trusted_private_chat",
            "is_direct": True,
            "invite": [RECIPIENT],
            "name": "clipy status",
            "topic": "Daily reports from clipy, the Jonathan Blow Clips agent",
        },
    )["room_id"]
    ROOM_FILE.write_text(room_id)
    return room_id


def send(text: str) -> None:
    room_id = urllib.parse.quote(room())
    txn = f"clipy-{time.time_ns()}"
    call(
        "PUT",
        f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
        {"msgtype": "m.text", "body": text},
    )


if __name__ == "__main__":
    send(Path(sys.argv[1]).read_text().strip())
