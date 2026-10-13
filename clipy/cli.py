"""clipy auth CHANNEL             sign a channel in to YouTube
clipy process CHANNEL URL      turn a source into queued clips by hand"""

import argparse
import json
import logging
import sys

from dotenv import load_dotenv

from .channel import PROJECT_ROOT, Channel, all_channels
from .logging_config import setup_logging
from .pipeline import process_source
from .studio import record
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
    studio = commands.add_parser(
        "studio-import", help="import observed Studio readings as JSON from stdin"
    )
    studio.add_argument("channel")
    collect = commands.add_parser(
        "studio-collect", help="collect fresh Studio analytics through direct API calls"
    )
    collect.add_argument("channel", nargs="?")
    comments = commands.add_parser(
        "comments", help="post and pin follow comments on new public uploads"
    )
    comments.add_argument("channel")
    commands.add_parser(
        "comments-serve", help="post and pin comments at scheduled release times"
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    setup_logging()
    if args.command == "comments-serve":
        from .comments import serve

        serve()
        return
    if args.command == "studio-collect":
        from .studio_collect import run

        if args.channel and args.channel not in {c.name for c in all_channels()}:
            parser.error("Unknown channel")
        sys.exit(run(args.channel))
    if args.channel not in {c.name for c in all_channels()}:
        parser.error(f"config.toml has no [channels.{args.channel}]")
    channel = Channel(args.channel)
    if args.command == "comments":
        from .comments import run

        result = run(channel)
        print(json.dumps(result))
        sys.exit(bool(result["errors"]))
    if args.command == "studio-import":
        print(json.dumps(record(channel, json.load(sys.stdin))))
        return
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
