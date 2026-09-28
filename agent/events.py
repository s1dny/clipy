"""Read `codex exec --json` events.

events.py stream              stdin JSONL -> readable log lines on stdout
events.py footer LOG SECONDS  the run's stats line; exits 1 if no clipy
                              tool call completed (Codex itself exits 0)
"""

import json
import sys


def events(lines):
    for line in lines:
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def stream() -> None:
    for line in sys.stdin:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(line.rstrip(), flush=True)
            continue
        item = event.get("item", {})
        if event["type"] == "item.completed" and item.get("type") == "agent_message":
            print(f"codex: {item['text']}", flush=True)
        elif event["type"] == "item.completed" and item.get("type") == "mcp_tool_call":
            error = f" {item['error']}" if item.get("error") else ""
            print(
                f"mcp: {item['server']}/{item['tool']} ({item['status']}){error}",
                flush=True,
            )
        elif event["type"] in {"turn.failed", "error"}:
            print(f"{event['type']}: {json.dumps(event)}", flush=True)


def duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def footer(log: str, seconds: int) -> int:
    with open(log, encoding="utf-8") as file:
        all_events = list(events(file))
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    usage["reasoning_output_tokens"] = 0
    for event in all_events:
        for key in usage:
            usage[key] += (event.get("usage") or {}).get(key, 0)
    calls = sum(
        1
        for e in all_events
        if e["type"] == "item.completed"
        and e.get("item", {}).get("type") == "mcp_tool_call"
        and e["item"].get("status") == "completed"
    )
    print(
        f"Run: {duration(seconds)} · "
        f"{usage['input_tokens']:,} input tokens ({usage['cached_input_tokens']:,} cached) · "
        f"{usage['output_tokens']:,} output tokens "
        f"({usage['reasoning_output_tokens']:,} reasoning) · "
        f"{calls} tool call{'' if calls == 1 else 's'}"
    )
    return 0 if calls else 1


if __name__ == "__main__":
    if sys.argv[1] == "stream":
        stream()
    else:
        sys.exit(footer(sys.argv[2], int(sys.argv[3])))
