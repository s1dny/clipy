#!/bin/sh
# One unattended run of clipy: Codex, signed in with a ChatGPT subscription,
# driving the clipy MCP server. CODEX_HOME must be persistent
# so the refreshed ChatGPT tokens in auth.json survive between runs.
set -eu
# pipefail: the run fails if codex fails, not just tee.
set -o pipefail 2>/dev/null || true

: "${CODEX_HOME:=/data/codex}"
: "${CLIPY_MCP_URL:=http://clipy.clipy.svc:8000/mcp}"
: "${CLIPY_MCP_TOKEN:?}"
export CLIPY_MCP_TOKEN
export CODEX_HOME
mkdir -p "$CODEX_HOME/runs" /tmp/agent

if [ ! -s "$CODEX_HOME/auth.json" ]; then
  echo "No ChatGPT login in $CODEX_HOME; run: codex login --device-auth" >&2
  exit 1
fi

log=/tmp/agent/events.jsonl
summary="$CODEX_HOME/runs/$(date -u +%Y-%m-%dT%H%MZ).md"
started=$(date +%s)
status=0
codex exec \
  --json \
  --skip-git-repo-check \
  --ephemeral \
  --cd /tmp/agent \
  --sandbox read-only \
  --model "${CODEX_MODEL:-gpt-6-sol}" \
  -c "model_reasoning_effort=\"${CODEX_REASONING:-medium}\"" \
  -c 'approval_policy="never"' \
  -c 'cli_auth_credentials_store="file"' \
  -c "mcp_servers.clipy.url=\"$CLIPY_MCP_URL\"" \
  -c 'mcp_servers.clipy.bearer_token_env_var="CLIPY_MCP_TOKEN"' \
  -c 'mcp_servers.clipy.required=true' \
  -c 'mcp_servers.clipy.default_tools_approval_mode="approve"' \
  -c 'mcp_servers.clipy.tool_timeout_sec=900' \
  --output-last-message "$summary" \
  - < "${CLIPY_AGENT_PROMPT:-/app/agent/prompt.md}" 2>&1 \
  | tee "$log" | python /app/agent/events.py stream || status=$?

# Codex exits 0 even when every tool call failed, so a run only counts if it
# reached the server; the same pass works out the stats line for the report.
stats=$(python /app/agent/events.py footer "$log" $(( $(date +%s) - started ))) \
  || { [ "$status" -eq 0 ] && status=1; }
echo "$stats"

# Tell Aiden how it went: the agent's closing summary, or why the run failed,
# followed by the stats line.
if [ -n "${MATRIX_ACCESS_TOKEN:-}" ]; then
  message=/tmp/agent/message.txt
  if [ "$status" -eq 0 ] && [ -s "$summary" ]; then
    cat "$summary" > "$message"
  else
    { echo "The clipy run failed (exit $status). Last events:"
      tail -n 15 "$log" | python /app/agent/events.py stream; } > "$message"
  fi
  printf '\n\n%s\n' "$stats" >> "$message"
  python /app/agent/notify.py "$message" || echo "clipy: Matrix notify failed" >&2
fi
exit "$status"
