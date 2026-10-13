#!/bin/sh
# The daily unattended runs of clipy: for each channel named on the command
# line, one Codex run (signed in with a ChatGPT subscription) driving that
# channel's clipy MCP endpoint; then an observer run that reads them all and
# writes the owner's report, sent over Matrix as one message under a stats
# header from the server. Runs go one after another, so they never share the
# ChatGPT login at once; one failing does not stop the rest. CODEX_HOME must
# be persistent so the refreshed tokens in auth.json survive.
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
: "${CLIPY_AGENT_WORK_DIR:=/tmp/agent}"
export CLIPY_MCP_TOKEN CLIPY_MCP_URL CODEX_HOME

if [ ! -s "$CODEX_HOME/auth.json" ]; then
  echo "No ChatGPT login in $CODEX_HOME; run: codex login --device-auth" >&2
  exit 1
fi
# codex exec with the settings every run shares; the prompt is stdin.
codex_run() {
  codex exec \
    --json \
    --skip-git-repo-check \
    --ephemeral \
    --dangerously-bypass-approvals-and-sandbox \
    --model "${CODEX_MODEL:-gpt-6.1-sol}" \
    -c "model_reasoning_effort=\"${CODEX_REASONING:-medium}\"" \
    -c 'cli_auth_credentials_store="file"' \
    -c 'web_search="live"' \
    "$@" - 2>&1
}

run_channel() {
  channel=$1
  work=$CLIPY_AGENT_WORK_DIR/$channel
  mkdir -p "$work"
  log=$work/events.jsonl
  started=$(date +%s)
  status=0
  echo "=== $channel"
  attempt=1
  while :; do
    status=0
    # Codex's own sandbox (bwrap) cannot create namespaces in the pod, so the
    # container is the sandbox: it mounts the data directory read-only except
    # for the Codex login and the headshots, which the agent downloads and crops
    # itself.
    { cat "$CLIPY_AGENT_DIR/prompt.md" "$CLIPY_AGENT_DIR/channels/$channel.md"
      if [ -n "${CLIPY_AGENT_NOTE:-}" ]; then
        printf '\nOne-off instruction from the owner for this run (it overrides the rules above where they conflict):\n%s\n' "$CLIPY_AGENT_NOTE"
      fi; } \
      | codex_run \
        --cd "$work" \
        -c "mcp_servers.clipy.url=\"$CLIPY_MCP_URL/$channel\"" \
        -c 'mcp_servers.clipy.bearer_token_env_var="CLIPY_MCP_TOKEN"' \
        -c 'mcp_servers.clipy.required=true' \
        -c 'mcp_servers.clipy.default_tools_approval_mode="approve"' \
        -c 'mcp_servers.clipy.tool_timeout_sec=900' \
      | tee "$log" | python "$CLIPY_AGENT_DIR/events.py" stream || status=$?

    if [ "$attempt" -ge 3 ] || ! python "$CLIPY_AGENT_DIR/events.py" retryable "$log"; then
      break
    fi
    echo "Codex usage limit before any tools ran; retrying $channel in 15 minutes (attempt $((attempt + 1))/3)."
    sleep "${CLIPY_QUOTA_RETRY_SECONDS:-900}"
    attempt=$((attempt + 1))
  done

  # Codex exits 0 even when every tool call failed, so a run only counts if it
  # reached the server; the same pass works out the run's stats line.
  python "$CLIPY_AGENT_DIR/events.py" footer "$log" $(( $(date +%s) - started )) \
    > "$work/footer" || status=1
  cat "$work/footer"
  [ "$status" -eq 0 ] || echo "This run failed (exit $status)." >> "$work/footer"
  return "$status"
}

# What the observer and the fallback report see of a channel's run.
run_summary() {
  echo "### $1"
  echo
  echo "Log:"
  python "$CLIPY_AGENT_DIR/events.py" stream < "$CLIPY_AGENT_WORK_DIR/$1/events.jsonl" | sed 's/^/    /'
  echo
  echo "Final message:"
  python "$CLIPY_AGENT_DIR/events.py" final "$CLIPY_AGENT_WORK_DIR/$1/events.jsonl"
  echo
  cat "$CLIPY_AGENT_WORK_DIR/$1/footer"
  echo
}

# The server builds the stats header, since only it can use the YouTube tokens.
stats_header() {
  python - <<'PY'
import os, urllib.request
request = urllib.request.Request(
    os.environ["CLIPY_MCP_URL"].rsplit("/mcp", 1)[0] + "/stats",
    headers={"Authorization": f"Bearer {os.environ['CLIPY_MCP_TOKEN']}"},
)
with urllib.request.urlopen(request, timeout=300) as response:
    print(response.read().decode())
PY
}

report() {
  work=$CLIPY_AGENT_WORK_DIR/observer
  mkdir -p "$work"
  stats_header > "$work/stats.md" \
    || echo "(The stats header is unavailable: the server did not answer.)" > "$work/stats.md"
  { cat "$CLIPY_AGENT_DIR/observer.md" "$work/stats.md"
    echo; echo "Runs:"; echo
    for channel in "$@"; do run_summary "$channel"; done; } > "$work/prompt.md"

  echo "=== observer"
  started=$(date +%s)
  codex_run --cd "$work" < "$work/prompt.md" > "$work/events.jsonl"
  python "$CLIPY_AGENT_DIR/events.py" final "$work/events.jsonl" > "$work/observer.md"
  observed=$(( $(date +%s) - started ))

  { cat "$work/stats.md"; echo; echo "---"; echo
    if [ -s "$work/observer.md" ] && ! grep -q '^Run failed:' "$work/observer.md"; then
      cat "$work/observer.md"
    else
      echo "Owner actions: could not be checked automatically (report generation failed)."
    fi
    echo; echo "---"
    for channel in "$@"; do printf '%s: %s  \n' "$channel" "$(head -n 1 "$CLIPY_AGENT_WORK_DIR/$channel/footer")"; done
    python "$CLIPY_AGENT_DIR/events.py" footer "$work/events.jsonl" "$observed" \
      | sed 's/^/observer: /; s/ · [0-9]* tool calls\{0,1\}$//'
  } > "$work/report.md"
  cat "$work/report.md"
}

failed=0
for channel in "$@"; do
  run_channel "$channel" || failed=1
done
report "$@"
# A Matrix outage never fails the run.
if [ -n "${MATRIX_ACCESS_TOKEN:-}" ]; then
  python -m clipy.matrix send "$CLIPY_AGENT_WORK_DIR/observer/report.md" \
    || echo "clipy: Matrix report failed" >&2
fi
exit "$failed"
