#!/bin/sh
# The daily unattended runs of clipy: for each channel named on the command
# line, one Codex run (signed in with a ChatGPT subscription) driving that
# channel's clipy MCP endpoint. Channels run one after another, so they never
# share the ChatGPT login at once; one failing does not stop the rest.
# CODEX_HOME must be persistent so the refreshed tokens in auth.json survive.
#
#   run.sh blow science philosophy
#
# CLIPY_AGENT_NOTE, if set, is appended to every channel's prompt as a
# one-off instruction for a manual run.
set -u
# pipefail: a run fails if codex fails, not just tee.
set -o pipefail 2>/dev/null || true

: "${CODEX_HOME:=/data/codex}"
: "${CLIPY_MCP_URL:=http://clipy.clipy.svc:8000/mcp}"
: "${CLIPY_DATA_DIR:=/data}"
: "${CLIPY_MCP_TOKEN:?}"
: "${CLIPY_AGENT_DIR:=/app/agent}"
export CLIPY_MCP_TOKEN CODEX_HOME

if [ ! -s "$CODEX_HOME/auth.json" ]; then
  echo "No ChatGPT login in $CODEX_HOME; run: codex login --device-auth" >&2
  exit 1
fi
# Speaker headshots, which the agent finds and crops itself.
mkdir -p "$CLIPY_DATA_DIR/headshots"

run_channel() {
  channel=$1
  work=/tmp/agent/$channel
  mkdir -p "$work"
  log=$work/events.jsonl
  started=$(date +%s)
  status=0
  echo "=== $channel"
  # The sandbox may reach the network (to download photos) and write only its
  # scratch directory and the headshots.
  { cat "$CLIPY_AGENT_DIR/prompt.md" "$CLIPY_AGENT_DIR/channels/$channel.md"
    if [ -n "${CLIPY_AGENT_NOTE:-}" ]; then
      printf '\nOne-off instruction from the owner for this run (it overrides the rules above where they conflict):\n%s\n' "$CLIPY_AGENT_NOTE"
    fi; } \
    | codex exec \
      --json \
      --skip-git-repo-check \
      --ephemeral \
      --cd "$work" \
      --sandbox workspace-write \
      --add-dir "$CLIPY_DATA_DIR/headshots" \
      --model "${CODEX_MODEL:-gpt-6-sol}" \
      -c "model_reasoning_effort=\"${CODEX_REASONING:-medium}\"" \
      -c 'approval_policy="never"' \
      -c 'cli_auth_credentials_store="file"' \
      -c 'sandbox_workspace_write.network_access=true' \
      -c 'web_search="live"' \
      -c "mcp_servers.clipy.url=\"$CLIPY_MCP_URL/$channel\"" \
      -c 'mcp_servers.clipy.bearer_token_env_var="CLIPY_MCP_TOKEN"' \
      -c 'mcp_servers.clipy.required=true' \
      -c 'mcp_servers.clipy.default_tools_approval_mode="approve"' \
      -c 'mcp_servers.clipy.tool_timeout_sec=900' \
      - 2>&1 \
    | tee "$log" | python "$CLIPY_AGENT_DIR/events.py" stream || status=$?

  # Codex exits 0 even when every tool call failed, so a run only counts if it
  # reached the server; the same pass works out the stats line for the report.
  stats=$(python "$CLIPY_AGENT_DIR/events.py" footer "$log" $(( $(date +%s) - started ))) \
    || status=1
  echo "$stats"

  # Append the stats line to the report the agent sent with send_report, or
  # report why the run failed. A Matrix outage never fails the run.
  if [ -n "${MATRIX_ACCESS_TOKEN:-}" ]; then
    failure=
    if [ "$status" -ne 0 ]; then
      failure=$work/failure.txt
      { echo "The clipy run failed. Last events:"; echo
        tail -n 15 "$log" | python "$CLIPY_AGENT_DIR/events.py" stream | sed 's/^/    /'; } > "$failure"
    fi
    python -m clipy.matrix finish "$channel" "$started" "$stats" $failure \
      || echo "clipy: Matrix report failed" >&2
  fi
  return "$status"
}

failed=0
for channel in "$@"; do
  run_channel "$channel" || failed=1
done
exit "$failed"
