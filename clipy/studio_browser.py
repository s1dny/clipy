"""Studio's native authenticated request service, inside the private browser pod.

No clicks or copied proof tokens: Google's own client performs session-risk and
attestation handling. The MCP server submits durable requests over the existing
shared data volume; Chromium's debugging socket stays pod-local.
"""

import fcntl
import logging
import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from .channel import data_root
from .common import read_json_file, write_json_file
from .studio_collect import ORIGIN

logger = logging.getLogger(__name__)
REQUEST = """async ({channelId, endpoint, payload}) => {
    const app = document.querySelector('ytcp-app');
    for (let i=0; i<300 && (!app?.secureRequestService || app.currentChannelId!==channelId); i++)
        await new Promise(resolve => setTimeout(resolve, 100));
    if (!app?.secureRequestService || app.currentChannelId!==channelId)
        throw new Error('Studio runtime unavailable or wrong channel; no request sent');
    const context = app.attestationManagerNetwork.baseContextFactory();
    if (context.user?.delegationContext?.externalChannelId!==channelId)
        throw new Error('Studio delegation does not match channel; no request sent');
    const version = app.attestationManagerNetwork.attApi.gapiVersion;
    return await Promise.race([app.secureRequestService.makeRequest({
        path: '/' + version + '/' + endpoint, httpMethod: 'POST',
        body: {...payload, context}
    }), new Promise((_, reject) => setTimeout(() => reject(new Error('Studio request timed out; inspect remote state')), 60000))]);
}"""


def execute(channel_id: str, endpoint: str, payload: dict) -> dict:
    if not re.fullmatch(r"[a-z_]+/[a-z_]+", endpoint) or "context" in payload:
        raise ValueError("Use a Studio resource/method; context comes from Studio.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            os.environ.get("CLIPY_STUDIO_CDP", "http://127.0.0.1:9222"),
            timeout=15_000,
        )
        page = None
        try:
            page = browser.contexts[0].new_page()
            page.set_default_timeout(60_000)
            page.goto(f"{ORIGIN}/channel/{channel_id}", wait_until="domcontentloaded")
            if page.url.split("/", 3)[:3] != ORIGIN.split("/", 3)[:3]:
                raise RuntimeError("Studio sign-in required; no request sent")
            page.locator("ytcp-app").wait_for(state="attached")
            response = page.evaluate(
                REQUEST,
                {"channelId": channel_id, "endpoint": endpoint, "payload": payload},
            )
            if not isinstance(response, dict) or "error" in response:
                raise RuntimeError(
                    "Studio runtime returned an error; inspect remote state"
                )
            if (
                response.get("responseContext", {})
                .get("webResponseContextExtensionData", {})
                .get("challenge")
            ):
                raise RuntimeError(
                    "Studio runtime requires a challenge; inspect remote state"
                )
            return response
        finally:
            if page is not None:
                page.close()
            # For an attached browser this disconnects our transport; owned tabs
            # are closed explicitly and the user's browser stays running.
            browser.close()


def submit(channel_id: str, endpoint: str, payload: dict) -> dict:
    folder = data_root() / "studio-requests"
    identity = f"{uuid.uuid4().hex}.json"
    path = folder / "pending" / identity
    result_path = folder / "results" / identity
    write_json_file(
        path,
        {
            "channel_id": channel_id,
            "endpoint": endpoint,
            "payload": payload,
            "status": "queued",
            "submitted_at": datetime.now(UTC).isoformat(),
        },
    )
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if not result_path.exists():
            threading.Event().wait(0.25)
            continue
        result = read_json_file(result_path)
        if result["status"] == "responded":
            return result["response"]
        if result["status"] == "unconfirmed":
            raise RuntimeError(result["error"])
        threading.Event().wait(0.25)
    raise TimeoutError(
        f"Studio request {path.stem} unconfirmed; inspect remote state before retrying"
    )


def serve() -> None:
    folder = data_root() / "studio-requests"
    for name in ("pending", "running", "results"):
        (folder / name).mkdir(parents=True, exist_ok=True)
    with (folder / ".worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in (folder / "running").glob("*.json"):
            result_path = folder / "results" / path.name
            if result_path.exists():
                path.unlink()
                continue
            value = read_json_file(path)
            value.update(
                status="unconfirmed",
                error="Studio worker restarted during request; inspect remote state before retrying",
            )
            write_json_file(result_path, value)
            path.unlink()
        Path("/tmp/studio-worker.pid").write_text(str(os.getpid()))
        logger.info("Studio request worker ready")
        while True:
            for path in sorted((folder / "pending").glob("*.json")):
                result_path = folder / "results" / path.name
                value = read_json_file(path)
                if value["status"] != "queued":
                    continue
                if (
                    datetime.now(UTC) - datetime.fromisoformat(value["submitted_at"])
                ).total_seconds() > 150:
                    value.update(
                        status="unconfirmed",
                        error="Studio request expired before execution; no request sent",
                    )
                    write_json_file(result_path, value)
                    path.unlink()
                    continue
                running = folder / "running" / path.name
                path.replace(running)
                path = running
                value.update(status="running", started_at=datetime.now(UTC).isoformat())
                write_json_file(path, value)
                try:
                    response = execute(
                        value["channel_id"], value["endpoint"], value["payload"]
                    )
                    value.update(status="responded", response=response)
                except Exception as exc:  # noqa: BLE001 - retain ambiguous outcomes, never replay
                    value.update(
                        status="unconfirmed", error=f"{type(exc).__name__}: {exc}"
                    )
                value["finished_at"] = datetime.now(UTC).isoformat()
                write_json_file(result_path, value)
                path.unlink()
            threading.Event().wait(0.25)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve()
