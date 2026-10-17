import logging
import os
import ssl
import time
from pathlib import Path

from .clips import chapters_text
from .queue import item_id

logger = logging.getLogger(__name__)

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
]


class YouTubeAuthError(RuntimeError):
    """Raised when there is no usable token and no one to consent interactively."""


def authorize(token_path: Path, handle: str) -> None:
    """Sign a channel in to YouTube: print a consent link, wait for Google's
    redirect to localhost, and keep the token only if it is for `handle`."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    secrets = Path(os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secrets.json"))
    if not secrets.exists():
        raise YouTubeAuthError(
            f"No OAuth client at {secrets}; download one from Google Cloud Console."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), YOUTUBE_SCOPES)
    creds = flow.run_local_server(
        port=0,
        open_browser=False,
        prompt="consent",
        authorization_prompt_message=f"Sign in as {handle}: {{url}}",
    )
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    mine = youtube.channels().list(part="snippet", mine=True).execute()["items"]
    signed_in = mine[0]["snippet"].get("customUrl", "") if mine else ""
    if signed_in.lower() != handle.lower():
        raise YouTubeAuthError(
            f"Signed in as {signed_in or 'no channel'}, not {handle}; "
            "pick that channel's brand account."
        )
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    logger.info("Saved %s's YouTube token to %s", handle, token_path)


def get_credentials(token_path: Path):
    """Load, refresh, and persist the cached token, which must hold every scope."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_path.exists():
        raise YouTubeAuthError(f"No YouTube token at {token_path}; run clipy auth.")

    creds = Credentials.from_authorized_user_file(str(token_path))
    missing = set(YOUTUBE_SCOPES) - set(creds.scopes or [])
    if missing:
        raise YouTubeAuthError(
            f"YouTube token lacks scopes {sorted(missing)}; run clipy auth again."
        )

    if not creds.valid:
        if not creds.refresh_token:
            raise YouTubeAuthError(
                "YouTube token has no refresh token; run clipy auth."
            )
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise YouTubeAuthError(
                f"YouTube token refresh failed ({exc}); run clipy auth again."
            ) from exc
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def get_youtube_service(token_path: Path):
    from googleapiclient.discovery import build

    return build(
        "youtube", "v3", credentials=get_credentials(token_path), cache_discovery=False
    )


def full_description(item: dict, description_template: str) -> str:
    """The clip's description with its chapters, wrapped in the channel template."""
    source_id = item_id(item).split(":")[0]
    body = item.get("description", "")
    if item.get("chapters"):
        body += "\n\nChapters\n" + chapters_text(item["chapters"])
    original_url = (
        f"https://youtube.com/watch?v={source_id}&t={item['start_ms'] // 1000}s"
    )
    return description_template.format(
        clip_description=body,
        original_url=original_url,
    )


def upload_video(
    youtube,
    item: dict,
    description_template: str,
    category_id: str,
    *,
    private: bool = False,
    on_uploaded=None,
):
    """Upload a queued clip with its thumbnail: private and scheduled when the
    item has `publish_at`, otherwise public straight away. Records the video
    id on the item; raises on failure."""
    from googleapiclient.http import MediaFileUpload
    from httplib2 import HttpLib2Error

    clip_path = Path(item["clip_path"])
    body = {
        "snippet": {
            "title": item["title"],
            "description": full_description(item, description_template),
            "categoryId": category_id,
            **({"tags": item["tags"]} if item.get("tags") else {}),
        },
        "status": (
            {"privacyStatus": "private", "publishAt": item["publish_at"]}
            if item.get("publish_at")
            else {"privacyStatus": "private" if private else "public"}
        ),
    }

    logger.info(
        "Uploading %s (%s)",
        item["title"],
        item.get("publish_at") or ("private" if private else "public"),
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(
            str(clip_path), mimetype="video/mp4", resumable=True, chunksize=10 << 20
        ),
    )
    response = None
    failures = 0
    while response is None:
        try:
            _status, response = request.next_chunk(num_retries=5)
            failures = 0
        except ConnectionError, TimeoutError, ssl.SSLError, HttpLib2Error:
            if failures >= 5:
                raise
            failures += 1
            logger.warning(
                "Upload connection interrupted; resuming the same session (attempt %s/5)",
                failures,
            )
            # next_chunk queries this session's received bytes after a transport
            # error, so retrying it cannot insert a second video.
            time.sleep(2 ** (failures - 1))
    item["youtube_id"] = response["id"]
    item["youtube_url"] = f"https://youtube.com/watch?v={response['id']}"
    logger.info("Uploaded %s", item["youtube_url"])
    if on_uploaded:
        on_uploaded(item)
    set_thumbnail(youtube, item)


def set_thumbnail(youtube, item: dict) -> bool:
    """Set an uploaded clip's thumbnail. On failure (YouTube rate-limits
    thumbnails per account) the item is marked thumbnail_pending, for the
    agent to retry on a later run (retry_thumbnails)."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    try:
        youtube.thumbnails().set(
            videoId=item["youtube_id"],
            media_body=MediaFileUpload(item["thumbnail_path"], mimetype="image/jpeg"),
        ).execute()
    except (HttpError, OSError) as exc:
        logger.warning("Could not set thumbnail (will retry): %s", exc)
        item["thumbnail_pending"] = True
        return False
    item.pop("thumbnail_pending", None)
    return True
