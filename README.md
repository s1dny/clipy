# Clipy

Clipy runs YouTube channels that post clips from long talks, podcasts and
streams. A Claude Code agent chooses the sources and excerpts and writes the
titles and descriptions. Clipy downloads, transcribes, renders and uploads.

| Channel | Speaker | Status |
|---|---|---|
| [Jonathan Blow Clips](https://www.youtube.com/@JonathanBlowClips) | Jonathan Blow | Running, monetized |
| [George Hotz Clips](https://www.youtube.com/@georgehotzclips) | George Hotz | Running |
| [DHH Clips](https://www.youtube.com/@DHHClipsDaily) | David Heinemeier Hansson | Paused |
| [Tsoding Clips](https://www.youtube.com/@TsodingClips) | Tsoding | Paused |

## How it works

`clipy serve` is an MCP server. Each channel has its own endpoint,
`/mcp/<channel>`, with tools to search YouTube, read transcripts, render and
upload clips, and call the Google APIs. Downloads, renders and uploads run as
background jobs that survive restarts.

`clipy agent` runs every day at 04:00 Sydney time. For each channel it starts
`claude -p` with [`agent/prompt.md`](agent/prompt.md) and the channel's policy
from [`agent/channels/`](agent/channels), connected to that channel's
endpoint. The agent keeps a notebook in the database between runs. When all
channels are done, [`agent/observer.md`](agent/observer.md) writes a report
that goes to Matrix.

`clipy studio` reads YouTube Studio analytics through a signed-in Chromium,
and sends the Studio requests the public API can't, such as turning on ads.

Every write to Google or Studio is logged before it's sent, and none are
retried automatically.

## Building

```
cargo build --release
```

Clipy needs ffmpeg and yt-dlp at runtime. `nix build` builds the package
production runs.

## Usage

```
clipy serve                      run the MCP server and jobs
clipy agent <channel>...         run the daily agents and send the report
clipy studio                     run the Studio worker
clipy collect [channel]          read Studio analytics once
clipy auth <channel>             sign a channel in to YouTube
clipy prepare <url>              download and transcribe a video
clipy transcript <url>           print a page of a transcript
clipy process <channel> <url>    render clips from a JSON selections file
clipy site                       serve the public site
```

To sign in a channel, enable the YouTube Data, Analytics and Reporting APIs
in a Google Cloud project, save a Desktop OAuth client as
`client_secrets.json`, and set the app to "In production" so the token
doesn't expire after a week. Then run `clipy auth <channel>` and choose the
channel's brand account.

## Configuration

Copy `config.toml.example` to `config.toml` and `.env.example` to `.env`.
`config.toml` holds the channels and models; `[channels.<name>]` overrides
`[defaults]`.

| Variable | |
|---|---|
| `CLIPY_DATA_DIR` | Database, clips and sources |
| `CLIPY_CONFIG` | Path to `config.toml` |
| `CLIPY_MCP_TOKEN` | Server bearer token, at least 32 characters |
| `ASSEMBLYAI_API_KEY` | Transcription |
| `OPENROUTER_API_KEY` | Thumbnail illustrations |
| `CLAUDE_CODE_OAUTH_TOKEN` | Agent login, from `claude setup-token` |
| `CLAUDE_MODEL`, `CLAUDE_EFFORT` | Agent model (default `opus`) and effort |
| `CLIPY_SERVER` | Server URL for the agent |
| `CLIPY_AGENT_NOTE` | Extra instruction for every channel's next run |
| `CLIPY_STUDIO_CDP` | Chromium DevTools URL (default `http://127.0.0.1:9222`) |
| `CLIPY_STUDIO_SESSION` | Saved Studio cookies |
| `MATRIX_HOMESERVER`, `MATRIX_ROOM`, `MATRIX_ACCESS_TOKEN` | Where the report goes |

## Deployment

Production runs on azalab-0 with the NixOS module in
[`nix/module.nix`](nix/module.nix). Settings and secrets are in the homelab
repo under `modules/apps/clipy.nix`. Each push to `main` is built in CI, then
the homelab updates its pin and azalab-0 switches to it.

To run the agents now:

```
ssh azalab-0 sudo systemctl start --no-block clipy-agent
```

## Tests

```
cargo test --release
```

The tests run the real binary with ffmpeg, SQLite and Chromium. Google,
Studio and OpenRouter are replaced by local servers behind a TLS proxy. Set
`CLIPY_TEST_CHROME` to the Chromium path.
