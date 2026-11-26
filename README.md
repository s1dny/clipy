# Clipy

Clipy runs YouTube clip channels with no human in the loop. Once a day a
Claude agent finds long-form talks, podcasts and streams, cuts them into 8 to
20 minute clips of one speaker, and keeps a week of uploads scheduled.

| Channel | Speaker | Goal | Status |
|---|---|---|---|
| [Jonathan Blow Clips](https://www.youtube.com/@JonathanBlowClips) | Jonathan Blow | Ad revenue | Running |
| [George Hotz Clips](https://www.youtube.com/@georgehotzclips) | George Hotz | Watch hours | Running |
| [DHH Clips](https://www.youtube.com/@DHHClipsDaily) | David Heinemeier Hansson | Watch hours | Paused |
| [Tsoding Clips](https://www.youtube.com/@TsodingClips) | Tsoding | Watch hours | Paused |

## How it works

Clipy is one Rust binary with three long-running parts:

- **`clipy serve`** exposes each channel's tools over MCP at `/mcp/<channel>`
  and runs the background jobs: downloading, transcribing (AssemblyAI),
  rendering (ffmpeg) and uploading. A channel's agent can only reach its own
  endpoint.
- **`clipy agent`** runs Claude Code headless (`claude -p`) once per channel,
  in parallel. The agent reads its channel's analytics and its notebook,
  finds sources, reads their transcripts, picks the excerpts, writes titles,
  descriptions and thumbnail concepts, and fixes underperforming uploads.
  Afterwards an observer agent writes one Matrix report with each channel's
  numbers and what the owner needs to do.
- **`clipy studio`** drives a signed-in browser to read YouTube Studio
  analytics hourly and to run Studio requests the public API can't, such as
  enabling ads.

The agent decides; the server enforces. Editorial choices live only in the
prompts, [`agent/prompt.md`](agent/prompt.md) and one policy per channel in
[`agent/channels/`](agent/channels). The server refuses releases that break
the contract: under eight minutes, fewer than two cuts, no opening title card
or explainer card, an em dash, or no `Source:` link in the description.

A few rules that matter when changing the code:

- **Writes are never retried.** Every Google and Studio mutation is journaled
  before it is sent. A failed write stays unconfirmed until the agent checks
  the remote state.
- **Uploads are never duplicated.** Jobs survive restarts; preparation and
  rendering resume, an interrupted upload is marked failed. Broken private
  uploads are replaced and then deleted; public videos never are.
- **Selections are immutable.** Re-rendering reuses saved selections;
  submitting different ones over existing exports is refused.
- **Google APIs are generic.** `google_api_spec`, `google_api_read` and
  `google_api_write` work from Google's discovery documents, with no
  per-endpoint wrappers.

## Usage

```
clipy serve                      MCP server and job runner
clipy agent <channel>...         daily agent run, then the report
clipy studio                     Studio collector and request worker
clipy collect [channel]          read Studio analytics once
clipy auth <channel>             sign a channel in to YouTube
clipy prepare <url>              download and transcribe a source
clipy transcript <url>           print a page of its transcript
clipy process <channel> <url>    render selections (--clips file.json)
clipy site                       serve the public site
```

Locally you need Rust, ffmpeg and yt-dlp:

```bash
cargo install --path .
cp config.toml.example config.toml
cp .env.example .env
clipy auth blow
clipy serve
```

`--clips` takes a JSON array in `process_source`'s schema. The CLI never picks
excerpts for you.

### Connecting a channel

Enable the YouTube Data, Analytics and Reporting APIs, save a Desktop OAuth
client as `client_secrets.json`, and publish the app "In production"
(testing tokens expire after 7 days). `clipy auth <channel>` prints a link;
pick the channel's brand account. The token is saved to
`channels/<channel>/youtube-token.json` only if it matches the channel's
`handle`.

## Configuration

`config.toml` holds everything that isn't a secret: models, and per channel
its handle, category, speaker, thumbnail template and prompts.
`[channels.<name>]` overrides `[defaults]`, and the agent's own prompt edits
override both.

| Variable | Used by | |
|---|---|---|
| `CLIPY_DATA_DIR` | all | Data directory |
| `CLIPY_CONFIG` | all | Path to `config.toml` |
| `CLIPY_MCP_TOKEN` | serve, agent | Bearer token, 32+ characters |
| `ASSEMBLYAI_API_KEY` | serve | Transcription |
| `OPENROUTER_API_KEY` | serve | Thumbnail illustrations |
| `CLIPY_TIMEZONE` | serve | Notebook timezone (`Australia/Sydney`) |
| `CLIPY_STUDIO_SESSION` | studio | Cookie checkpoint |
| `CLIPY_STUDIO_CDP` | studio | Browser DevTools (`http://127.0.0.1:9222`) |
| `CLAUDE_CODE_OAUTH_TOKEN` | agent | From `claude setup-token` |
| `CLAUDE_MODEL`, `CLAUDE_EFFORT` | agent | Model (`opus`) and effort |
| `CLIPY_SERVER` | agent | Server URL |
| `CLIPY_AGENT_NOTE` | agent | One-off instruction for every channel |
| `CLIPY_QUOTA_RETRY_SECONDS` | agent | Wait after a usage limit (900, three tries) |
| `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM` | agent | Report destination; no token, no report |
| `MATRIX_HOMESERVER` | agent | `https://matrix.aza.network` |
| `YOUTUBE_CLIENT_SECRETS` | auth | OAuth client (`client_secrets.json`) |

The data directory holds `clipy.db` (SQLite: notebooks, queue, selections,
transcripts, jobs, journals, analytics), `channels/<channel>/` (token, clips,
artwork), `sources/<id>/` (media, deleted after rendering) and `headshots/`.

## Deployment

Clipy runs on azalab-0 under the NixOS module in [`nix/module.nix`](nix/module.nix);
its settings and secrets live in the homelab repo (`modules/apps/clipy.nix`).
Pushing to `main` builds the flake in CI, the homelab moves its pin, and
azalab-0 switches. The agent runs daily at 04:00 Sydney time for the channels
the homelab lists. To run it now:

```bash
ssh azalab-0 sudo systemctl start --no-block clipy-agent
```

## Development

```bash
cargo fmt
cargo clippy --all-targets
cargo test --release
```

The tests run the real binary end to end against real ffmpeg, SQLite and a
real Chromium (`CLIPY_TEST_CHROME`). Google, Studio and OpenRouter are local
fakes behind a TLS-intercepting proxy, so production URLs are used unchanged.
