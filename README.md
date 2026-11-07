# Clipy

Clipy runs YouTube clip channels without a person in the loop. It takes
long-form videos such as talks, lectures, podcasts, interviews and streams,
cuts them into standalone clips of 8 to 20 minutes that each feature one
speaker, and publishes them. Each channel has one goal: ad revenue, or watch
hours if it is not monetized yet.

| Channel | Who appears | Goal |
|---|---|---|
| [Jonathan Blow Clips](https://www.youtube.com/@JonathanBlowClips) (`blow`) | Jonathan Blow only | Ad revenue |
| [DHH Clips](https://www.youtube.com/@DHHClipsDaily) (`dhh`) | David Heinemeier Hansson only | Watch hours |
| [George Hotz Clips](https://www.youtube.com/@georgehotzclips) (`hotz`) | George Hotz only | Watch hours |
| [Tsoding Clips](https://www.youtube.com/@TsodingClips) (`tsoding`) | Tsoding only | Watch hours (paused) |

## What happens each day

Once a day an AI agent runs every channel, side by side. It reads the channel's
analytics, looks at how recent uploads are doing, and decides what to change.
It finds new source videos, checks that the right person does most of the
talking, and clips them. Every run it keeps the next seven days scheduled,
one upload a day, with a reserve of clips behind them. It rewrites titles and thumbnails on videos that underperform,
and learns which excerpts and titles work from the results.

The agent has no memory of its own between runs. It reads a notebook at the
start of each run and updates it at the end: its strategy, the experiments in
flight, what it learned from the numbers, and candidate sources to clip next.

After every channel has run, a second agent reads what each one did and
writes the owner's actions, which are sent over Matrix in one message under a
header of each channel's earnings, views and subscribers.

## How it is built

Clipy is one Rust binary, `clipy`, with a subcommand for each part.

**The agent** is Claude Code, run headless (`claude -p`) on the owner's
Claude subscription by `clipy agent`. Its instructions are
[`agent/prompt.md`](agent/prompt.md), the routine every channel follows, plus
a short policy for the channel in [`agent/channels/`](agent/channels): what
it optimises for, who may appear, where to look for sources, and what the
owner has to do by hand. These rules live only in the prompts; changing how a
channel behaves means editing its policy file. The agent can search the web,
read images and run shell commands in its container.

**The server** (`clipy serve`) gives the agent its tools as MCP and does the
actual work. Each channel in `config.toml` has its own endpoint at
`/mcp/<channel>`, so an agent can only touch its own channel. The tools cover:

- general Google API access driven by Google's published discovery documents,
  plus raw dated counter snapshots and Studio readings
- searching for source videos and checking who speaks in them
- preparing and rendering sources, and publishing and repairing uploads, as
  persisted background jobs
- editing queued clips, rendering artwork, and Studio requests for live videos
- reading and changing the channel's editorial guidance
- reading and writing the notebook

The observer gets `/observer`: one read-only SQL tool over everything Clipy
keeps.

The API interface has no per-endpoint wrappers or hand-written metric
catalogue. `google_api_spec` searches the official discovery document and
returns exact subtrees by JSON Pointer. `google_api_read` and
`google_api_write` call the named method with its own parameters and return
the raw response; read/write separation comes from each method's HTTP verb.
Media uploads are restricted to the channel's own clips and artwork. Every
mutation is journaled before it is sent; a failed write stays unconfirmed
until the agent checks the remote state, and nothing retries writes.

**The agent owns the editorial work.** It starts `prepare_source` to download
and transcribe a source with AssemblyAI, reads the complete speaker-labelled,
timestamped transcript with `get_source_transcript`, and chooses the excerpts
with their titles, descriptions, chapters, edits and thumbnail concepts. It
submits them to `process_source`; the server only validates the render
contract, renders with ffmpeg, draws illustrations with the image model and
queues the clips. No transcript or clip text goes to another text model.
Transcripts are shared between channels, so a source is only paid for once.

Saved selections are immutable: retrying a render reuses them, and
submitting different ones over existing exports is refused. An empty
selection records a source with nothing worth clipping. Jobs run in three
lanes shared by every channel, each one job at a time: downloading and
transcribing, rendering, and YouTube uploads and changes. They survive
restarts: preparation and rendering resume, while an interrupted upload is
failed so it is never duplicated.

**Rendering** is one continuous ffmpeg encode with aligned audio and video.
The agent may keep chosen source ranges, add image-and-caption cards (square
corners, monospace type, an optional real image with its credit) and hold an
inspected source frame over continuing audio. A verified stream date and the
source's name stay on screen throughout. Every new video has at least two
cuts, opens with a card naming its title or concept, and has at least one
later card explaining a term or concept; publishing refuses one that doesn't. Every release is at least eight
minutes after cuts, has no em dashes, and its description carries a literal
`Source:` line with the original link. `inspect_source_frame`,
`inspect_clip_frame` and `get_edit_report` let the agent check its edits.

**Thumbnails** have a picture on the left and, on the white right side, an
illustration of the clip's idea drawn by an image model through OpenRouter.
For Blow, DHH and Hotz the picture is a fixed template in `thumbnails/`;
the other channels use a headshot of the speaker: the agent finds a photo
and saves it, cropped, with the `headshot` tool, which shows it the exact
thumbnail panel. Headshots live under `headshots/` and are shared by every
channel.

**Studio.** `clipy studio` runs in the private browser pod. Every hour it
reads Studio's analytics directly over HTTP with the signed-in session's
cookies: daily channel series in AUD for the report header and since-published
readings for recent and experimented-on videos. It refreshes its client
context, then reads the browser's cookie jar once, on an authentication
failure, and saves the jar only after an authenticated, channel-checked
response. It also executes the agent's `studio_api_request`s through
Studio's own signed-in request service in a tab of its own, so Google's
client handles session risk and attestation. That is how the bot enables ads
on Jonathan Blow's videos and verifies them. Requests are journaled and an
unconfirmed one is never replayed.

Upload success is distinct from processing success. Clipy keeps each MP4
until YouTube confirms it processed, and synchronises the queue with
YouTube's calendar hourly. Failed or stalled private uploads are repaired by
re-encoding the excerpt, uploading a private replacement, confirming it
processed, transferring the schedule and deleting the broken copy. Public,
watched, working and rejected videos are never replaced.

## Data

Everything Clipy keeps is under the data directory (`/srv/clipy` on
azalab-0):

- `clipy.db`: SQLite with the notebooks, queue, selections, transcripts,
  jobs, API and Studio journals, counter snapshots and Studio readings
- `channels/<channel>/`: the YouTube token, rendered clips and artwork
- `sources/<video id>/`: downloaded source media, removed once rendered
- `headshots/`: speaker photos shared by every channel

## Production

Forgejo CI runs one job on azalab-0 (`scripts/deliver.sh`) within a
300-second budget: it builds the test image with a persistent Cargo cache,
runs the E2E suite, then builds and pushes `git.aza.network/aza/clipy` from
every `main` commit. Flux deploys it to azalab-0, and CI waits for
`/releasez` to report that the server, site, Studio worker and agent all run
the commit. The manifests are in the homelab repo under
`flux/clusters/azalab-0/manifests/apps/clipy`.

The `clipy-agent` CronJob runs the agents every day at 04:00 Sydney time,
for the channels named in its command. Tsoding is paused: it is left out, so
its agent makes and schedules nothing new. To run them now:

```bash
ssh azalab-0 kubectl -n clipy create job --from=cronjob/clipy-agent agent-manual-$(date +%s)
```

The agent signs in with a long-lived token from `claude setup-token`, kept
as `CLAUDE_CODE_OAUTH_TOKEN` in the `clipy-claude` secret. Replace it the
same way if it ever expires.

## Configuration

`config.toml` holds every setting that is not a secret: the image and speech
models, and for each channel its handle, YouTube category, speaker,
thumbnail template and prompt templates. `[defaults]` applies to every
channel and `[channels.<name>]` overrides it; the agent's own prompt edits
win over both. Start from `config.toml.example`.

| Variable | Used by | Meaning |
|---|---|---|
| `CLIPY_DATA_DIR` | everything | The data directory (`/data` in the image) |
| `CLIPY_CONFIG` | everything | Path to `config.toml` |
| `CLIPY_MCP_TOKEN` | server, agent | Bearer token for the server, at least 32 characters |
| `ASSEMBLYAI_API_KEY` | server | Transcription |
| `OPENROUTER_API_KEY` | server | Thumbnail illustrations |
| `CLIPY_TIMEZONE` | server | Timezone for notebook entries (default `Australia/Sydney`) |
| `CLIPY_STUDIO_SESSION` | studio | The private cookie checkpoint |
| `CLIPY_STUDIO_CDP` | studio | The browser's DevTools endpoint (default `http://127.0.0.1:9222`) |
| `CLAUDE_CODE_OAUTH_TOKEN` | agent | The Claude subscription login |
| `CLAUDE_MODEL`, `CLAUDE_EFFORT` | agent | Model (default `opus`) and optional effort level |
| `CLIPY_SERVER` | agent | The server (default `http://clipy.clipy.svc:8000`) |
| `CLIPY_AGENT_NOTE` | agent | A one-off instruction added to every channel's prompt |
| `CLIPY_QUOTA_RETRY_SECONDS` | agent | Wait before retrying a usage limit hit before any tool ran (default 900, three attempts) |
| `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM` | agent | The report bot's token and room; without a token no report is sent |
| `MATRIX_HOMESERVER` | agent | Matrix homeserver URL (defaults to the one in the cluster) |
| `YOUTUBE_CLIENT_SECRETS` | `clipy auth` | Google OAuth client file (default `client_secrets.json`) |

### Connecting a YouTube channel

Enable the YouTube Data, Analytics and Reporting APIs in the Google Cloud
project, create a Desktop OAuth client saved as `client_secrets.json`, and
keep the app "In production" (testing logins expire after 7 days). Then:

```bash
clipy auth <channel>
```

Open the printed link, pick the channel's brand account and allow access.
The token is saved to `channels/<channel>/youtube-token.json`, only if it
belongs to the channel's `handle`. Copy it to the server with:

```bash
ssh azalab-0 "kubectl -n clipy exec -i deploy/clipy -- sh -c 'cat > /data/channels/<channel>/youtube-token.json && chmod 600 /data/channels/<channel>/youtube-token.json'" < channels/<channel>/youtube-token.json
```

## Running it locally

Clipy needs Rust, ffmpeg and yt-dlp. For manual work:

```bash
cargo install --path .
clipy prepare <youtube-url>
clipy transcript <youtube-url> --offset 0
clipy process <channel> <youtube-url> --clips selections.json [--speaker "Full Name"]
```

`--clips` is a JSON array matching `process_source`'s schema; omit it to
resume saved selections. These commands never choose excerpts for you.

## Development

```bash
cargo fmt
cargo clippy --all-targets
cargo test --release
```

The suite runs the real binary end to end: CLI, MCP and browser workflows,
real ffmpeg exports, SQLite and HTTP clients. Google, Studio, OpenRouter and
Kubernetes are local providers behind a TLS-intercepting proxy, so the app
uses its production URLs unchanged. It covers rendering and resume, channel
isolation, interrupted uploads, private-upload repair, analytics collection,
Studio requests through a real Chromium, daily-agent retries and release
readiness. The Studio test needs Chrome or Chromium (`CLIPY_TEST_CHROME`).
