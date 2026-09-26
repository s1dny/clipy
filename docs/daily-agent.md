# Daily channel agent

Every day at 4:00 AM Sydney time, the `clipper-agent` CronJob on azalab-0 runs
[`agent/run.sh`](../agent/run.sh): Codex CLI (`codex exec`), signed in with a
ChatGPT subscription rather than an API key, follows
[`agent/prompt.md`](../agent/prompt.md) using the clipper MCP server's tools.

- The MCP server is reached in-cluster at
  `http://clipper.clipper.svc:8000/mcp/$CLIPPER_MCP_SECRET`, and its tools are
  pre-approved (`default_tools_approval_mode = "approve"`), so nothing waits on
  a person. The agent has no shell access beyond a read-only sandbox.
- `CODEX_HOME` is `/data/codex` on the clipper volume (`/srv/clipper/codex` on
  the host): the ChatGPT login in `auth.json`, which Codex refreshes in place,
  and each run's final message in `runs/`.
- The agent's model and effort come from `CODEX_MODEL` and `CODEX_REASONING`
  on the CronJob.

## Signing in

The server has its own ChatGPT session. Copying `auth.json` from a laptop
would share one rotating refresh token between two machines, and whichever
refreshed second would be signed out. Sign in once with a device code:

```bash
ssh -t azalab-0 kubectl -n clipper exec -it deploy/clipper -- env CODEX_HOME=/data/codex codex login --device-auth
```

Open the printed link, enter the code, and approve. If ChatGPT refuses device
codes, turn on device code authorization for Codex in ChatGPT's security
settings first.

## Operating it

Run it now:

```bash
ssh azalab-0 kubectl -n clipper create job --from=cronjob/clipper-agent agent-manual-$(date +%s)
```

Logs are in `kubectl -n clipper logs job/<name>`, past run summaries in
`/srv/clipper/codex/runs/` on azalab-0, and the agent's own notebook behind the
`read_notes` tool.

## What the server enforces regardless of the prompt

- `publish_clip` refuses if anything was uploaded earlier the same Sydney day,
  if a publish job is already queued, or if another channel video goes live on
  the same Sydney day.
- `process_source` refuses sources already clipped (local work directories,
  queue, and every `Original:` link in the channel's descriptions) and runs
  one source at a time.
