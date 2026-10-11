#!/bin/sh
# Restart the adapter when Docker reports it unhealthy. A restart policy only restarts a container
# that exited; one that is running but no longer answering needs this.
set -eu
cd "${MMA_ROOT:-/opt/muse-adapter}"
id=$(docker compose ps -q adapter 2>/dev/null || true)
[ -n "$id" ] || exit 0
status=$(docker inspect --format '{{.State.Status}}' "$id")
health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")
if [ "$status" = running ] && [ "$health" = unhealthy ]; then
  logger -t muse-adapter-heal "the adapter is unhealthy, restarting it"
  docker compose restart adapter
fi
