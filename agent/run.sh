#!/bin/sh
# One unattended run of the channel agent: Codex, signed in with a ChatGPT
# subscription, driving the clipper MCP server. CODEX_HOME must be persistent
# so the refreshed ChatGPT tokens in auth.json survive between runs.
set -eu
# pipefail: the run fails if codex fails, not just tee.
set -o pipefail 2>/dev/null || true

: "${CODEX_HOME:=/data/codex}"
: "${CLIPPER_MCP_URL:=http://clipper.clipper.svc:8000/mcp/${CLIPPER_MCP_SECRET:?}}"
export CODEX_HOME
mkdir -p "$CODEX_HOME/runs" /tmp/agent

if [ ! -s "$CODEX_HOME/auth.json" ]; then
  echo "No ChatGPT login in $CODEX_HOME; run: codex login --device-auth" >&2
  exit 1
fi

log=/tmp/agent/run.log
summary="$CODEX_HOME/runs/$(date -u +%Y-%m-%dT%H%MZ).md"
status=0
codex exec \
  --skip-git-repo-check \
  --ephemeral \
  --cd /tmp/agent \
  --sandbox read-only \
  --model "${CODEX_MODEL:-gpt-6-astra}" \
  -c "model_reasoning_effort=\"${CODEX_REASONING:-high}\"" \
  -c 'approval_policy="never"' \
  -c 'cli_auth_credentials_store="file"' \
  -c "mcp_servers.clipper.url=\"$CLIPPER_MCP_URL\"" \
  -c 'mcp_servers.clipper.required=true' \
  -c 'mcp_servers.clipper.default_tools_approval_mode="approve"' \
  -c 'mcp_servers.clipper.tool_timeout_sec=900' \
  --output-last-message "$summary" \
  - < "${CLIPPER_AGENT_PROMPT:-/app/agent/prompt.md}" 2>&1 | tee "$log" || status=$?

# Codex exits 0 even when every tool call failed, so judge the run by whether
# it actually reached the clipper server.
if [ "$status" -eq 0 ] && ! grep -q '^mcp: clipper/.* (completed)' "$log"; then
  echo "clipper-agent: no clipper tool call completed; failing the run" >&2
  status=1
fi

# Tell Aiden how it went: the agent's closing summary, or why the run failed.
if [ -n "${MATRIX_ACCESS_TOKEN:-}" ]; then
  if [ "$status" -ne 0 ] || [ ! -s "$summary" ]; then
    summary=/tmp/agent/failure.txt
    { echo "The Jonathan Blow Clips agent run failed (exit $status). Last log lines:"
      tail -n 15 "$log"; } > "$summary"
  fi
  python /app/agent/notify.py "$summary" || echo "clipper-agent: Matrix notify failed" >&2
fi
exit "$status"
