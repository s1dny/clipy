import logging
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rich.progress import (
    BarColumn,
    DownloadColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .common import write_json_file
from .logging_config import create_progress
from .queue import load_queue

logger = logging.getLogger(__name__)

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
PUBLISH_HOUR_UTC = int(os.environ.get("PUBLISH_HOUR_UTC", "12"))
YOUTUBE_CATEGORY_ID = os.environ.get("YOUTUBE_CATEGORY_ID", "22")


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
                logger.warning("YouTube token refresh failed; reauthorizing: %s", exc)
                creds = None
                try:
                    token_path.unlink()
                except FileNotFoundError:
                    pass

        if not creds or not creds.valid:
            if not client_secrets.exists():
                logger.error("YouTube client secrets not found at %s", client_secrets)
                logger.error("Download OAuth 2.0 credentials from Google Cloud Console")
                logger.error("and save as client_secrets.json in the project root.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secrets), YOUTUBE_SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=False)

        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def upload_single_video(
    youtube, item: dict, thumbnail_path: Path, description_template: str
) -> bool:
    """Upload a single video to YouTube as unlisted."""
    from googleapiclient.http import MediaFileUpload

    title = item["title"]
    video_path = item.get("clip_path")

    if not video_path or not Path(video_path).exists():
        logger.warning(
            "Skipping %r: video file not found at %s",
            title,
            video_path,
            extra={"highlighter": None},
        )
        return False

    logger.info("Uploading: %s...", title, extra={"highlighter": None})
    logger.info("Using thumbnail: %s", thumbnail_path)

    # Extract original video ID from clip_path: .../output/<video_id>/clips/...
    clip_parts = Path(video_path).parts
    try:
        clips_idx = clip_parts.index("clips")
        source_video_id = clip_parts[clips_idx - 1]
    except ValueError, IndexError:
        source_video_id = None
    original_url = (
        f"https://youtube.com/watch?v={source_video_id}" if source_video_id else ""
    )

    description = description_template.format(
        clip_description=item.get("description", ""),
        original_url=original_url,
    )

    # Schedule for the assigned date at 12:00 UTC if available
    scheduled_date = item.get("scheduled_date")
    if scheduled_date:
        publish_at = f"{scheduled_date}T{PUBLISH_HOUR_UTC:02d}:00:00Z"
        status_body = {
            "privacyStatus": "private",
            "publishAt": publish_at,
        }
        logger.info("Scheduled to publish: %s", scheduled_date)
    else:
        status_body = {
            "privacyStatus": "unlisted",
        }

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": YOUTUBE_CATEGORY_ID,
        },
        "status": status_body,
    }

    file_size = Path(video_path).stat().st_size
    media = MediaFileUpload(
        video_path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024
    )

    try:
        request = youtube.videos().insert(
            part="snippet,status", body=body, media_body=media
        )
        response = None
        with create_progress(
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            DownloadColumn(binary_units=True),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as pbar:
            task_id = pbar.add_task(f"Uploading {title}", total=file_size)
            while response is None:
                status, response = request.next_chunk()
                if status:
                    pbar.update(
                        task_id,
                        completed=min(file_size, int(status.progress() * file_size)),
                    )
            pbar.update(task_id, completed=file_size)

        video_id = response["id"]
        item["youtube_id"] = video_id
        item["youtube_url"] = f"https://youtube.com/watch?v={video_id}"
        logger.info("Done: %s", item["youtube_url"])

        # Set thumbnail
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
            ).execute()
            logger.info("Thumbnail set")
        except Exception as e:
            logger.warning("Could not set thumbnail: %s", e)

        return True

    except Exception as e:
        logger.error("Upload failed for %r: %s", title, e, extra={"highlighter": None})
        return False


def _parse_scheduled_date(scheduled_date: str | None) -> date | None:
    if not scheduled_date:
        return None

    try:
        return datetime.fromisoformat(scheduled_date).date()
    except ValueError:
        return None


def _publish_at_for_date(scheduled_day: date) -> datetime:
    return datetime.combine(
        scheduled_day,
        datetime.min.time(),
        tzinfo=timezone.utc,
    ) + timedelta(hours=PUBLISH_HOUR_UTC)


def _next_schedulable_date(now: datetime) -> date:
    today = now.date()
    if _publish_at_for_date(today) > now:
        return today
    return today + timedelta(days=1)


def upload_from_queue(
    queue_file: Path, description_template: str, project_root: Path | None = None
):
    """Process the upload queue: assign dates and upload due videos."""
    if not queue_file.exists():
        logger.warning("No upload queue found. Run the pipeline first.")
        return

    queue = load_queue(queue_file)

    pending = [q for q in queue if q["status"] == "pending"]
    if not pending:
        logger.info("No pending uploads in queue.")
        return

    missing_metadata = [item for item in pending if not item.get("thumbnail_path")]
    if missing_metadata:
        logger.error(
            "Cannot upload: some pending items are missing 'thumbnail_path' metadata."
        )
        logger.error(
            "Re-run each source video with --thumbnail and --step 5/6, "
            "or manually edit upload_queue.json to add thumbnail_path."
        )
        for item in missing_metadata[:10]:
            logger.error(
                "  Missing metadata: %s",
                item["title"],
                extra={"highlighter": None},
            )
        return

    # Assign scheduled dates to pending items starting from the earliest future slot.
    now = datetime.now(timezone.utc)
    minimum_date = _next_schedulable_date(now)
    reserved_dates = set()
    pending_to_assign = []
    pending_ids = {id(item) for item in pending}

    for item in pending:
        scheduled_day = _parse_scheduled_date(item.get("scheduled_date"))
        if scheduled_day is None:
            pending_to_assign.append(item)
            continue

        if _publish_at_for_date(scheduled_day) <= now:
            logger.info(
                "Rescheduling past-due upload '%s' from %s",
                item["title"],
                item["scheduled_date"],
            )
            pending_to_assign.append(item)
            continue

        reserved_dates.add(scheduled_day)

    for item in queue:
        if id(item) in pending_ids:
            continue
        scheduled_day = _parse_scheduled_date(item.get("scheduled_date"))
        if scheduled_day is None:
            continue
        if _publish_at_for_date(scheduled_day) > now:
            reserved_dates.add(scheduled_day)

    unscheduled = [item for item in pending_to_assign if not item.get("scheduled_date")]
    random.shuffle(unscheduled)
    to_reassign = [
        item for item in pending_to_assign if item.get("scheduled_date")
    ] + unscheduled

    next_date = minimum_date
    updated_schedule = False
    for item in to_reassign:
        while next_date in reserved_dates:
            next_date += timedelta(days=1)

        if item.get("scheduled_date") != next_date.isoformat():
            item["scheduled_date"] = next_date.isoformat()
            updated_schedule = True

        reserved_dates.add(next_date)
        next_date += timedelta(days=1)

    if updated_schedule:
        write_json_file(queue_file, queue)

    # Upload all pending clips sequentially
    logger.info("%s clip(s) to upload:", len(pending))
    for item in pending:
        logger.info(
            "  %s: %s",
            item.get("scheduled_date", "unscheduled"),
            item["title"],
            extra={"highlighter": None},
        )

    missing_thumbnail_files = []
    resolved_thumbnails: dict[str, Path] = {}
    for item in pending:
        clip_path = item.get("clip_path")
        thumbnail_path = item.get("thumbnail_path")
        if not clip_path:
            missing_thumbnail_files.append(
                (item.get("title", "<unknown>"), "Missing clip_path")
            )
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
            logger.error("  %s: %s", title, err, extra={"highlighter": None})
        return

    # Authenticate with YouTube
    youtube = get_youtube_service(queue_file.parent, project_root)

    for item in pending:
        thumbnail_path = resolved_thumbnails[item["clip_path"]]
        success = upload_single_video(
            youtube, item, thumbnail_path, description_template
        )
        if success:
            item["status"] = "uploaded"
            item["uploaded_at"] = datetime.now(timezone.utc).isoformat()
        else:
            item["status"] = "failed"

        # Save after each upload
        write_json_file(queue_file, queue)

    uploaded = sum(1 for item in pending if item["status"] == "uploaded")
    logger.info("Uploaded %s/%s clip(s)", uploaded, len(pending))
