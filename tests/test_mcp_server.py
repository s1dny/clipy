"""Edit two independent channels through the real MCP HTTP endpoint."""

import requests
from PIL import Image
from support import (
    BLUE,
    HEADSHOT_RED,
    ROOT,
    call,
    data,
    image,
    near,
    text,
)


def test_channels_keep_notes_clips_and_artwork_separate(server):
    base, root = server
    assert requests.post(base + "/mcp/blow", timeout=3).status_code == 401
    assert (
        requests.get(
            base + "/stats", headers={"Authorization": "Bearer wrong"}, timeout=3
        ).status_code
        == 401
    )
    call(server, "write_notes", {"content": "Jai experiment"})
    call(
        server, "write_notes", {"content": "Neuroscience experiment"}, channel="science"
    )
    assert "Jai" not in text(call(server, "read_notes", channel="science"))
    assert "Neuroscience" not in text(call(server, "read_notes"))

    call(
        server,
        "edit_clip",
        {
            "clip_id": "aaaaaaaaaaa:02",
            "title": "Programming",
            "thumbnail_prompt": "a binary",
        },
    )
    call(server, "discard_clip", {"clip_id": "aaaaaaaaaaa:03", "reason": "off topic"})
    assert [(c["id"], c["title"]) for c in data(call(server, "list_queue"))] == [
        ("aaaaaaaaaaa:02", "Programming")
    ]
    assert [c["id"] for c in data(call(server, "list_queue", channel="science"))] == [
        "bbbbbbbbbbb:01"
    ]
    thumbnail = image(call(server, "view_thumbnail", {"clip_id": "aaaaaaaaaaa:02"}))
    template = Image.open(ROOT / "thumbnails/blow.jpg").convert("RGB")
    assert near(thumbnail.getpixel((300, 300)), template.getpixel((300, 300)))
    assert near(thumbnail.getpixel((1274, 540)), BLUE)

    headshot = root / "headshots/robert-sapolsky.jpg"
    headshot.parent.mkdir(exist_ok=True)
    Image.new("RGB", (1600, 900), HEADSHOT_RED).save(headshot)
    call(
        server,
        "edit_clip",
        {"clip_id": "bbbbbbbbbbb:01", "thumbnail_prompt": "a neuron"},
        channel="science",
    )
    thumbnail = image(
        call(server, "view_thumbnail", {"clip_id": "bbbbbbbbbbb:01"}, "science")
    )
    assert near(thumbnail.getpixel((360, 540)), HEADSHOT_RED)
    assert near(thumbnail.getpixel((1370, 540)), BLUE)
    assert data(call(server, "list_queue"))[0]["title"] == "Programming"
