import logging
import os
import sys
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


def _client_secrets_path(project_root: Path) -> Path:
    configured = os.environ.get("YOUTUBE_CLIENT_SECRETS")
    return Path(configured) if configured else project_root / "client_secrets.json"


def authorize(token_path: Path, project_root: Path) -> None:
    """Run the OAuth consent flow in a browser and cache the token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_secrets_path = _client_secrets_path(project_root)
    if not client_secrets_path.exists():
        logger.error("YouTube client secrets not found at %s", client_secrets_path)
        logger.error("Download OAuth 2.0 credentials from Google Cloud Console")
        logger.error("and save as client_secrets.json in the project root.")
        sys.exit(1)
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secrets_path), YOUTUBE_SCOPES
    )
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Saved YouTube token to %s", token_path)


def get_credentials(token_path: Path):
    """Load, refresh, and persist the cached token, which must hold every scope."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    seed = os.environ.get("YOUTUBE_TOKEN_JSON")
    if not token_path.exists() and seed:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(seed, encoding="utf-8")

    if not token_path.exists():
        raise YouTubeAuthError(f"No YouTube token at {token_path}; run 'auth'.")

    creds = Credentials.from_authorized_user_file(str(token_path))
    missing = set(YOUTUBE_SCOPES) - set(creds.scopes or [])
    if missing:
        raise YouTubeAuthError(
            f"YouTube token lacks scopes {sorted(missing)}; run 'auth' again."
        )

    if not creds.valid:
        if not creds.refresh_token:
            raise YouTubeAuthError("YouTube token has no refresh token; run 'auth'.")
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise YouTubeAuthError(
                f"YouTube token refresh failed ({exc}); run 'auth' again."
            ) from exc
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def get_youtube_service(token_path: Path):
    from googleapiclient.discovery import build

    return build(
        "youtube", "v3", credentials=get_credentials(token_path), cache_discovery=False
    )


def get_analytics_service(token_path: Path):
    from googleapiclient.discovery import build

    return build(
        "youtubeAnalytics",
        "v2",
        credentials=get_credentials(token_path),
        cache_discovery=False,
    )


def full_description(item: dict, description_template: str) -> str:
    """The clip's description with its chapters, wrapped in the channel template."""
    source_id = item_id(item).split(":")[0]
    body = item.get("description", "")
    if item.get("chapters"):
        body += "\n\nChapters\n" + chapters_text(item["chapters"])
    return description_template.format(
        clip_description=body,
        original_url=f"https://youtube.com/watch?v={source_id}",
    )


def upload_video(youtube, item: dict, description_template: str, category_id: str):
    """Upload a queued clip with its thumbnail: private and scheduled when the
    item has `publish_at`, otherwise public straight away. Records the video
    id on the item; raises on failure."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

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
            else {"privacyStatus": "public"}
        ),
    }

    logger.info("Uploading %s (%s)", item["title"], item.get("publish_at") or "public")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(
            str(clip_path), mimetype="video/mp4", resumable=True, chunksize=10 << 20
        ),
    )
    response = None
    while response is None:
        _status, response = request.next_chunk()
    item["youtube_id"] = response["id"]
    item["youtube_url"] = f"https://youtube.com/watch?v={response['id']}"
    logger.info("Uploaded %s", item["youtube_url"])

    try:
        youtube.thumbnails().set(
            videoId=response["id"],
            media_body=MediaFileUpload(item["thumbnail_path"], mimetype="image/jpeg"),
        ).execute()
    except (HttpError, OSError) as exc:
        logger.warning("Could not set thumbnail: %s", exc)
