from clipper.cli import main
from clipper.clips import (
    clip_folder_name,
    cut_clip,
    cut_clips,
    extract_clips,
)
from clipper.common import (
    extract_video_id,
    load_or_compute,
    openrouter_request,
    run_cmd,
)
from clipper.download import download_video
from clipper.logging_config import setup_logging
from clipper.queue import resolve_upload_thumbnail_path, save_queue
from clipper.thumbnails import (
    composite_thumbnail,
    generate_illustration,
    generate_thumbnails,
)
from clipper.transcription import build_sentence_transcript, transcribe_audio
from clipper.upload import (
    YOUTUBE_SCOPES,
    get_youtube_service,
    upload_from_queue,
    upload_single_video,
)


if __name__ == "__main__":
    main()
