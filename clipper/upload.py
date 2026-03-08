import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import write_json_file
from .queue import load_queue

logger = logging.getLogger("clipper")

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_youtube_service(work_root: Path, project_root: Path | None = None):
    """Authenticate with YouTube API and return service object."""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent

    token_path = work_root / ".youtube_token.json"
    client_secrets = Path(
        os.environ.get("YOUTUBE_CLIENT_SECRETS", project_root / "client_secrets.json")
    )

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request

            try:
                creds.refresh(Request())
            except RefreshError as exc:
                logger.warning(f"YouTube token refresh failed; reauthorizing: {exc}")
                creds = None
                try:
                    token_path.unlink()
                except FileNotFoundError:
                    pass

        if not creds or not creds.valid:
            if not client_secrets.exists():
                logger.error(f"YouTube client secrets not found at {client_secrets}")
                logger.error("Download OAuth 2.0 credentials from Google Cloud Console")
                logger.error("and save as client_secrets.json in the project root.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secrets), YOUTUBE_SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=False)

        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def upload_single_video(youtube, item: dict, thumbnail_path: Path) -> bool:
    """Upload a single video to YouTube as unlisted."""
    from googleapiclient.http import MediaFileUpload

    title = item["title"]
    video_path = item.get("clip_path")

    if not video_path or not Path(video_path).exists():
        logger.warning(f"Skipping '{title}': video file not found at {video_path}")
        return False

    logger.info(f"Uploading: {title}...")
    logger.info(f"Using thumbnail: {thumbnail_path}")

    # Extract original video ID from clip_path: .../output/<video_id>/clips/...
    clip_parts = Path(video_path).parts
    try:
        clips_idx = clip_parts.index("clips")
        source_video_id = clip_parts[clips_idx - 1]
    except (ValueError, IndexError):
        source_video_id = None
    original_url = (
        f"https://youtube.com/watch?v={source_video_id}" if source_video_id else ""
    )

    description = (
        f"Support me on Ko-fi: https://ko-fi.com/jonathanblowclips\n"
        f"{item.get('description', '')}\n"
        f"\n"
        f"Clip from Jonathan Blow\n"
        f"Original: {original_url}\n"
        f"Twitch: https://twitch.tv/j_blow\n"
        f"YouTube: https://www.youtube.com/@jblow888\n"
        f"\n"
        f"#jonathanblow #gamedev #webdevelopment #programming #jai"
    )

    # Schedule for the assigned date at 00:00 UTC if available
    scheduled_date = item.get("scheduled_date")
    if scheduled_date:
        publish_at = f"{scheduled_date}T12:00:00Z"
        status_body = {
            "privacyStatus": "private",
            "publishAt": publish_at,
        }
        logger.info(f"Scheduled to publish: {scheduled_date}")
    else:
        status_body = {
            "privacyStatus": "unlisted",
        }

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": status_body,
    }

    from tqdm import tqdm

    file_size = Path(video_path).stat().st_size
    media = MediaFileUpload(
        video_path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024
    )

    try:
        request = youtube.videos().insert(
            part="snippet,status", body=body, media_body=media
        )
        response = None
        with tqdm(
            total=file_size, unit="B", unit_scale=True, desc="    Uploading"
        ) as pbar:
            while response is None:
                prev = pbar.n
                status, response = request.next_chunk()
                if status:
                    pbar.update(int(status.progress() * file_size) - prev)

        video_id = response["id"]
        item["youtube_id"] = video_id
        item["youtube_url"] = f"https://youtube.com/watch?v={video_id}"
        logger.info(f"Done: {item['youtube_url']}")

        # Set thumbnail
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(
                    str(thumbnail_path), mimetype="image/jpeg"
                ),
            ).execute()
            logger.info("Thumbnail set")
        except Exception as e:
            logger.warning(f"Could not set thumbnail: {e}")

        return True

    except Exception as e:
        logger.error(f"Upload failed for '{title}': {e}")
        return False


def upload_from_queue(queue_file: Path, project_root: Path | None = None):
    """Process the upload queue: assign dates and upload due videos."""
    if not queue_file.exists():
        logger.warning("No upload queue found. Run the pipeline first.")
        return

    queue = load_queue(queue_file)

    pending = [q for q in queue if q["status"] == "pending"]
    if not pending:
        logger.info("No pending uploads in queue.")
        return

    missing_metadata = [
        item
        for item in pending
        if not item.get("thumbnail_path")
    ]
    if missing_metadata:
        logger.error(
            "Cannot upload: some pending items are missing 'thumbnail_path' metadata."
        )
        logger.error(
            "Re-run each source video with --thumbnail and --step 5/6, "
            "or manually edit upload_queue.json to add thumbnail_path."
        )
        for item in missing_metadata[:10]:
            logger.error(f"  Missing metadata: {item['title']}")
        return

    # Assign scheduled dates to unscheduled items (random order, one per day at 00:00 UTC)
    unscheduled = [q for q in pending if not q.get("scheduled_date")]
    if unscheduled:
        random.shuffle(unscheduled)
        existing_dates = [
            datetime.fromisoformat(q["scheduled_date"]).date()
            for q in queue
            if q.get("scheduled_date")
        ]
        if existing_dates:
            next_date = max(existing_dates) + timedelta(days=1)
        else:
            next_date = datetime.now(timezone.utc).date() + timedelta(days=1)

        for item in unscheduled:
            item["scheduled_date"] = next_date.isoformat()
            next_date += timedelta(days=1)

        write_json_file(queue_file, queue)

    # Upload all pending clips sequentially
    logger.info(f"{len(pending)} clip(s) to upload:")
    for item in pending:
        logger.info(f"  {item.get('scheduled_date', 'unscheduled')}: {item['title']}")

    missing_thumbnail_files = []
    resolved_thumbnails: dict[str, Path] = {}
    for item in pending:
        clip_path = item.get("clip_path")
        thumbnail_path = item.get("thumbnail_path")
        if not clip_path:
            missing_thumbnail_files.append((item.get("title", "<unknown>"), "Missing clip_path"))
            continue

        if clip_path in resolved_thumbnails:
            continue

        try:
            if not thumbnail_path:
                raise FileNotFoundError("Missing thumbnail_path")
            resolved = Path(thumbnail_path)
            if not resolved.exists():
                raise FileNotFoundError(f"Thumbnail file not found at {resolved}")
            resolved_thumbnails[clip_path] = resolved
        except FileNotFoundError as e:
            missing_thumbnail_files.append((item.get("title", "<unknown>"), str(e)))

    if missing_thumbnail_files:
        logger.error("Cannot upload: referenced thumbnail files are missing.")
        for title, err in missing_thumbnail_files:
            logger.error(f"  {title}: {err}")
        return

    # Authenticate with YouTube
    youtube = get_youtube_service(queue_file.parent, project_root)

    for item in pending:
        thumbnail_path = resolved_thumbnails[item["clip_path"]]
        success = upload_single_video(youtube, item, thumbnail_path)
        if success:
            item["status"] = "uploaded"
            item["uploaded_at"] = datetime.now(timezone.utc).isoformat()
        else:
            item["status"] = "failed"

        # Save after each upload
        write_json_file(queue_file, queue)

    uploaded = sum(1 for item in pending if item["status"] == "uploaded")
    logger.info(f"Uploaded {uploaded}/{len(pending)} clip(s)")
