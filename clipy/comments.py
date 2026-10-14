"""Post and pin the owner's follow comment once each new upload goes public."""

import fcntl
import logging
import os
import re
import threading
from datetime import UTC, datetime, timedelta

from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import sync_playwright

from .channel import all_channels
from .common import read_json_file, write_json_file
from .queue import load_queue

logger = logging.getLogger(__name__)
FOLLOW_COMMENT = "feel free to follow me on x :) https://x.com/aidensidny"


def comment_and_pin(video_id: str, handle: str, state: dict, save) -> None:
    """Select the owner in an isolated browser login, then verify the saved pin."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            os.environ.get("CLIPY_STUDIO_CDP", "http://127.0.0.1:9222"),
            timeout=10_000,
        )
        context = None
        try:
            context = browser.new_context(
                storage_state=browser.contexts[0].storage_state()
            )
            page = context.new_page()
            page.set_default_timeout(20_000)
            page.goto("https://www.youtube.com/channel_switcher")
            page.get_by_text(handle, exact=True).click()
            page.goto(
                f"https://www.youtube.com/watch?v={video_id}",
                wait_until="domcontentloaded",
            )
            page.get_by_role("button", name="Account menu", exact=True).click()
            page.locator("ytd-popup-container").get_by_text(
                handle, exact=True
            ).wait_for()
            page.keyboard.press("Escape")
            comments = page.locator("ytd-comments#comments")
            comments.scroll_into_view_if_needed()
            comments.get_by_text("Add a comment...", exact=True).wait_for()
            row = (
                comments.locator("ytd-comment-thread-renderer")
                .filter(has=page.get_by_text(FOLLOW_COMMENT, exact=True))
                .filter(has=page.get_by_text(handle, exact=True))
                .first
            )
            # Check newest comments too, so a prior successful submission whose
            # response was lost can be found before submitting again.
            try:
                row.wait_for(timeout=5_000)
            except BrowserTimeout:
                comments.get_by_text("Sort by", exact=True).click()
                page.locator("ytd-popup-container").get_by_text(
                    "Newest first", exact=True
                ).click()
                try:
                    row.wait_for(timeout=5_000)
                except BrowserTimeout:
                    pass
            if not row.count():
                if state.get("post_started_at"):
                    raise RuntimeError(
                        "Comment submission is uncertain; inspect before posting again"
                    )
                comments.get_by_text("Add a comment...", exact=True).click()
                comments.locator('#contenteditable-root[contenteditable="true"]').fill(
                    FOLLOW_COMMENT
                )
                state["post_started_at"] = datetime.now(UTC).isoformat()
                save()
                comments.get_by_role("button", name="Comment", exact=True).click()
                row.wait_for()
            state["posted_at"] = state.get("posted_at") or datetime.now(UTC).isoformat()
            save()
            row.get_by_role("button", name="Action menu", exact=True).click()
            popup = page.locator("ytd-popup-container")
            popup.get_by_text(re.compile(r"^(?:Unpin|Pin)$")).wait_for()
            if popup.get_by_text("Unpin", exact=True).is_visible():
                page.keyboard.press("Escape")
            else:
                popup.get_by_text("Pin", exact=True).click()
                dialog = page.locator("ytd-confirm-dialog-renderer")
                dialog.get_by_role("button", name="Pin", exact=True).click()
            # Pinning is asynchronous. Wait for YouTube to show the result
            # before navigating, so a reload cannot cancel the pin request.
            row.get_by_text(f"Pinned by {handle}", exact=True).wait_for()
            page.reload(wait_until="domcontentloaded")
            comments.scroll_into_view_if_needed()
            row.get_by_text(f"Pinned by {handle}", exact=True).wait_for()
        finally:
            if context:
                context.close()
            browser.close()


def run(channel, *, respect_retry: bool = False) -> dict:
    """Serialize the CLI and collector so they cannot post the same comment."""
    channel.dir.mkdir(parents=True, exist_ok=True)
    with (channel.dir / "follow-comments.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"pinned": 0, "errors": []}
        return _run(channel, respect_retry=respect_retry)


def _run(channel, *, respect_retry: bool = False) -> dict:
    """Retry unfinished new uploads; private/scheduled videos stay untouched."""
    path = channel.dir / "follow-comments.json"
    states = read_json_file(path) if path.exists() else {}
    now = datetime.now(UTC)
    targets = [
        video_id
        for video_id, due in pending(channel, states, respect_retry=respect_retry)
        if due <= now
    ]
    errors, pinned = [], 0
    if not targets:
        return {"pinned": 0, "errors": []}
    youtube = channel.youtube()
    channel_id = (
        youtube.channels().list(part="id", mine=True).execute()["items"][0]["id"]
    )
    for video_id in targets:
        state = states.setdefault(video_id, {})
        try:
            videos = (
                youtube.videos()
                .list(part="status,snippet", id=video_id)
                .execute()["items"]
            )
            if not videos or videos[0]["status"]["privacyStatus"] != "public":
                state["next_attempt_at"] = (
                    datetime.now(UTC) + timedelta(minutes=10)
                ).isoformat()
                write_json_file(path, states)
                continue
            if videos[0]["snippet"]["channelId"] != channel_id:
                raise ValueError("Upload does not belong to the authenticated channel")
            comment_and_pin(
                video_id,
                channel.config().channel.handle,
                state,
                lambda: write_json_file(path, states),
            )
            state["pinned_at"] = datetime.now(UTC).isoformat()
            state.pop("error", None)
            state.pop("next_attempt_at", None)
            pinned += 1
        except Exception as exc:  # noqa: BLE001 - retry one failure without blocking other uploads
            state["error"] = type(exc).__name__
            state["next_attempt_at"] = (
                datetime.now(UTC) + timedelta(minutes=10)
            ).isoformat()
            errors.append({"video_id": video_id, "error": type(exc).__name__})
            logger.warning(
                "Follow comment incomplete for %s/%s: %s",
                channel.name,
                video_id,
                type(exc).__name__,
            )
        write_json_file(path, states)
    return {"pinned": pinned, "errors": errors}


def pending(
    channel, states: dict, *, respect_retry: bool = True
) -> list[tuple[str, datetime]]:
    """Local release deadlines; this never contacts YouTube."""
    # Follow comments are authorized only for Jonathan Blow Clips, including
    # when old queues still contain flags from the previous broad rollout.
    if channel.name != "blow":
        return []
    deadlines = {}
    for item in load_queue(channel.queue_file):
        video_id = item.get("youtube_id")
        state = states.get(video_id, {})
        if (
            not video_id
            or not item.get("follow_comment_pending")
            or state.get("pinned_at")
        ):
            continue
        due = (
            datetime.fromisoformat(item["publish_at"])
            if item.get("publish_at")
            else datetime.min.replace(tzinfo=UTC)
        )
        if respect_retry and state.get("next_attempt_at"):
            due = max(due, datetime.fromisoformat(state["next_attempt_at"]))
        deadlines[video_id] = due
    return list(deadlines.items())


def serve() -> None:
    """Wake at release times or filesystem changes, and resume after restarts."""
    from watchfiles import watch

    channels = all_channels()
    for channel in channels:
        channel.dir.mkdir(parents=True, exist_ok=True)
    wake, stop = threading.Event(), threading.Event()

    def changed():
        try:
            for _ in watch(
                *(channel.dir for channel in channels),
                recursive=False,
                watch_filter=lambda _change, path: path.endswith(
                    ("upload_queue.json", "follow-comments.json")
                ),
                stop_event=stop,
                debounce=50,
                step=50,
                force_polling=False,
            ):
                wake.set()
        except Exception:
            logger.exception("Comment queue watcher failed")
        finally:
            stop.set()
            wake.set()

    watcher = threading.Thread(target=changed, name="comment-queue-events", daemon=True)
    watcher.start()
    unavailable = {}
    try:
        while not stop.is_set():
            wake.clear()
            deadlines = []
            for channel in channels:
                try:
                    path = channel.dir / "follow-comments.json"
                    states = read_json_file(path) if path.exists() else {}
                    times = [due for _, due in pending(channel, states)]
                    if not times:
                        continue
                    due = max(
                        min(times),
                        unavailable.get(channel.name, datetime.min.replace(tzinfo=UTC)),
                    )
                    if due <= datetime.now(UTC):
                        result = run(channel, respect_retry=True)
                        write_json_file(
                            channel.dir / "follow-comments-status.json",
                            {
                                **result,
                                "checked_at": datetime.now(UTC).isoformat(),
                                "trigger": "scheduled_release",
                            },
                        )
                        states = read_json_file(path) if path.exists() else {}
                        times = [due for _, due in pending(channel, states)]
                        unavailable.pop(channel.name, None)
                    deadlines.extend(
                        max(
                            t,
                            unavailable.get(
                                channel.name, datetime.min.replace(tzinfo=UTC)
                            ),
                        )
                        for t in times
                    )
                except Exception:
                    logger.exception(
                        "Comment scheduler failed for %s; retrying in ten minutes",
                        channel.name,
                    )
                    unavailable[channel.name] = datetime.now(UTC) + timedelta(
                        minutes=10
                    )
                    deadlines.append(unavailable[channel.name])
            if deadlines:
                delay = max(0.1, (min(deadlines) - datetime.now(UTC)).total_seconds())
                logger.info("Next scheduled comment attempt in %.1f seconds", delay)
            else:
                delay = None
                logger.info("Waiting for newly uploaded clips")
            wake.wait(delay)
    finally:
        stop.set()
        watcher.join(timeout=6)
