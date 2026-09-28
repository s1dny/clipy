"""clipy auth CHANNEL             sign a channel in to YouTube
clipy process CHANNEL URL      turn a source into queued clips by hand"""

import argparse
import logging
import sys

from dotenv import load_dotenv

from .channel import PROJECT_ROOT, Channel, all_channels
from .logging_config import setup_logging
from .pipeline import process_source
from .upload import authorize

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(prog="clipy", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="sign a channel in to YouTube")
    auth.add_argument("channel")
    process = commands.add_parser("process", help="clip one source video")
    process.add_argument("channel")
    process.add_argument("url")
    process.add_argument("--speaker", help="the source's main speaker")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    setup_logging()
    if args.channel not in {c.name for c in all_channels()}:
        parser.error(f"config.toml has no [channels.{args.channel}]")
    channel = Channel(args.channel)
    config = channel.config()

    if args.command == "auth":
        authorize(channel.token_file, config.channel.handle)
        return
    speaker = args.speaker or config.channel.speaker
    if not speaker:
        parser.error("this channel has no fixed speaker; pass --speaker")
    try:
        process_source(channel, args.url, speaker)
    except Exception:
        logger.exception("Processing failed; run it again to resume.")
        sys.exit(1)
