# Clipy

Clipy turns long videos into a YouTube clip channel. You give it a channel
and a speaker. Once a day, a Claude Code agent finds that person's talks,
podcasts and streams, picks excerpts that stand on their own, and schedules
them as uploads of 8 to 20 minutes.

The agent makes the editorial calls: which sources to use, where to cut, and
what the titles, descriptions and thumbnails say. Clipy downloads, transcribes,
renders, uploads and keeps the records.

## How it works

Clipy is one Rust binary that runs as three services.

`clipy serve` is an MCP server with one endpoint per channel,
`/mcp/<channel>`. Its tools search YouTube, read transcripts, render and upload
clips, and call the Google APIs. Downloads, renders and uploads run as
background jobs that survive a restart.

`clipy agent` runs `claude -p` once for each channel, connected to that
channel's endpoint. The instructions are [`agent/prompt.md`](agent/prompt.md)
followed by the channel's `policy` from `config.toml`. The agent keeps notes
from one run to the next. When every channel has finished, a second agent
sends a short report to Matrix.

`clipy studio` uses a signed-in Chromium to read YouTube Studio analytics.
It also sends the requests the public API doesn't offer, such as turning on
ads.

## Installation

Clipy needs Rust, ffmpeg, yt-dlp and [Claude Code](https://claude.com/claude-code),
and runs `ffmpeg`, `ffprobe`, `yt-dlp` and `claude` from PATH.

```
cargo install --path .
```

Nix users can run `nix build` instead. The flake also provides a NixOS
module.

## Getting started

1. Copy `config.toml.example` to `config.toml` and add a `[channels.<name>]`
   table for your channel.
2. Copy `.env.example` to `.env` and fill in the keys.
3. In Google Cloud, enable the YouTube Data, Analytics and Reporting APIs.
   Create a Desktop OAuth client, save it as `client_secrets.json`, and set
   the app to "In production" so tokens don't expire after a week.
4. Sign the channel in with `clipy auth <name>`.
5. Start the server with `clipy serve`, then run the agent with
   `clipy agent <name>`.

To clip one video by hand:

```
clipy prepare <url>                                 # download and transcribe
clipy transcript <url>                              # read the transcript
clipy process <name> <url> --clips selections.json  # render the clips
```

## Configuration

`config.toml` defines the models and the channels. Each `[channels.<name>]`
table sets the channel's handle, speaker, YouTube category, thumbnail
template, prompts and `policy`. It inherits anything it leaves out from
`[defaults]`. Thumbnail templates and speaker headshots are kept in the data
directory.

| Variable | |
|---|---|
| `CLIPY_DATA_DIR` | Database, media and credentials (default `.`) |
| `CLIPY_CONFIG` | Path to `config.toml` |
| `CLIPY_MCP_TOKEN` | Server bearer token, at least 32 characters |
| `ASSEMBLYAI_API_KEY` | Transcription |
| `OPENROUTER_API_KEY` | Thumbnail illustrations |
| `CLAUDE_CODE_OAUTH_TOKEN` | Agent login, from `claude setup-token` |
| `CLAUDE_MODEL` | Agent model (default `opus`) |
| `CLIPY_SERVER` | Server URL for the agent (default `http://127.0.0.1:8000`) |
| `CLIPY_STUDIO_CDP` | Chromium DevTools URL (default `http://127.0.0.1:9222`) |
| `MATRIX_HOMESERVER`, `MATRIX_ROOM`, `MATRIX_ACCESS_TOKEN` | Report destination |

## NixOS

```nix
{
  imports = [ clipy.nixosModules.default ];

  services.clipy = {
    enable = true;
    configFile = ./clipy.toml;
    environmentFiles = [ "/run/secrets/clipy.env" ];
    agentChannels = [ "example" ];
  };
}
```

The module runs the server and the agent, which starts at 04:00 every day. It
runs the Studio collector when `studioCdp` is set and a static site when
`siteDir` is set. See [`nix/module.nix`](nix/module.nix) for every option.

## Development

```
cargo fmt
cargo clippy --all-targets
cargo test --release
```

The tests run the real binary against ffmpeg, SQLite and Chromium. Google,
Studio and OpenRouter are replaced by local servers behind a TLS proxy. Set
`CLIPY_TEST_CHROME` if Chromium isn't found automatically.
