#!/usr/bin/env bash
set -euo pipefail
# One host job: cached toolchain, full parallel E2E, immutable image, live proof.
revision=${GITHUB_SHA:?}
run_number=${GITHUB_RUN_NUMBER:?}
ci_image="clipy-ci:${revision:0:12}-${run_number}"
image="git.aza.network/aza/clipy:main-${revision:0:12}-${run_number}"
started=$(date +%s)
if [[ ${GITHUB_EVENT_NAME:-} != pull_request ]]; then
  created=$(curl --fail --silent --show-error --config <(printf 'header = "Authorization: token %s"\n' "$FORGEJO_TOKEN") \
    "http://127.0.0.1:3000/api/v1/repos/${GITHUB_REPOSITORY}/actions/runs" \
    | jq -er --arg sha "$revision" '.workflow_runs | map(select(.commit_sha == $sha and .status == "running")) | first | .created')
  started=$(date -d "$created" +%s)
fi
budget() {
  local elapsed=$(( $(date +%s) - started ))
  printf 'DELIVERY elapsed=%ss stage=%s revision=%s\n' "$elapsed" "$1" "$revision"
  if (( elapsed >= 300 )); then
    echo 'Commit-to-ready exceeded the 300-second budget' >&2
    exit 1
  fi
}
config_dir=$(mktemp -d)
cleanup() { rm -rf "$config_dir"; docker image rm "$ci_image" >/dev/null 2>&1 || true; }
trap cleanup EXIT
export DOCKER_CONFIG="$config_dir" DOCKER_BUILDKIT=1
budget build-check-image
docker build --target ci --tag "$ci_image" .
budget full-e2e
docker run --rm --cpus=4 --memory=4g --shm-size=512m "$ci_image" \
  timeout 60s sh -c 'ruff format --check . && ruff check . && pytest -n 4 --dist loadgroup -q --durations=10 tests'
if [[ ${GITHUB_EVENT_NAME:-} == pull_request ]]; then exit 0; fi
budget build-runtime
docker build --target runtime --build-arg "CLIPY_REVISION=$revision" --tag "$image" .
printf '%s' "$REGISTRY_TOKEN" | docker login git.aza.network --username aiden --password-stdin
docker push "$image"
budget wait-for-production
# Only the app reads Kubernetes with its namespace-scoped workload identity.
# CI receives the current commit and aggregate readiness, never cluster credentials.
while true; do
  budget production-readiness
  if response=$(curl --fail --silent --show-error --max-time 10 "$CLIPY_RELEASE_URL") &&
    jq -e --arg revision "$revision" '.revision == $revision and .ready == true' <<< "$response" >/dev/null; then
    budget verified-live
    printf 'COMMIT_TO_READY_SECONDS=%s\n' "$(( $(date +%s) - started ))"
    break
  fi
  sleep 2
done
