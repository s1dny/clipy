#!/bin/sh
# One unattended run of the channel agent: Codex, signed in with a ChatGPT
# subscription, driving the clipper MCP server. CODEX_HOME must be persistent
# so the refreshed ChatGPT tokens in auth.json survive between runs.
set -eu

: "${CODEX_HOME:=/data/codex}"
: "${CLIPPER_MCP_URL:=http://clipper.clipper.svc:8000/mcp/${CLIPPER_MCP_SECRET:?}}"
export CODEX_HOME
mkdir -p "$CODEX_HOME/runs" /tmp/agent

if [ ! -s "$CODEX_HOME/auth.json" ]; then
  echo "No ChatGPT login in $CODEX_HOME; run: codex login --device-auth" >&2
  exit 1
fi

exec codex exec \
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
  -c 'mcp_servers.clipper.tool_timeout_sec=300' \
  --output-last-message "$CODEX_HOME/runs/$(date -u +%Y-%m-%dT%H%MZ).md" \
  - < "${CLIPPER_AGENT_PROMPT:-/app/agent/prompt.md}"
