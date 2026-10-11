#!/bin/sh
# Back up the adapter's state into ./backups and keep the newest MMA_BACKUP_KEEP (default 7).
# Run daily by muse-adapter-backup.timer. The backup is consistent while the adapter keeps serving.
set -eu
cd "${MMA_ROOT:-/opt/muse-adapter}"
exec docker compose exec -T adapter muse-mcp-adapter \
  --database /data/adapter.db --config /config/serve.json \
  backup --to /backups --keep "${MMA_BACKUP_KEEP:-7}"
