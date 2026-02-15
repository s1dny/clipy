#!/bin/bash

# Clipper - YouTube Clip Automation
# Usage: ./clipper.sh --channel <name> --thumbnail <file.jpg> <youtube-url>
#    or: ./clipper.sh --channel <name> upload

usage() {
    echo "Usage: ./clipper.sh --channel <name> --thumbnail <file.jpg> <youtube-url>"
    echo "   or: ./clipper.sh --channel <name> upload"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
channels_root="$script_dir/channels"
channel=""
args=()

while [ $# -gt 0 ]; do
    case "$1" in
        --channel)
            if [ -z "${2:-}" ]; then
                echo "Error: --channel requires a value"
                usage
                exit 1
            fi
            channel="$2"
            shift 2
            ;;
        --channel=*)
            channel="${1#*=}"
            if [ -z "$channel" ]; then
                echo "Error: --channel requires a value"
                usage
                exit 1
            fi
            shift
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done

if [ ${#args[@]} -eq 0 ]; then
    usage
    exit 1
fi

if [ -z "$channel" ]; then
    echo "Error: --channel is required"
    usage
    exit 1
fi

if [[ "$channel" == *"/"* || "$channel" == "." || "$channel" == ".." ]]; then
    echo "Error: --channel must be a single directory name under ./channels"
    exit 1
fi

export YT_CLIP_WORK_DIR="$channels_root/$channel"

uv run main.py "${args[@]}"
