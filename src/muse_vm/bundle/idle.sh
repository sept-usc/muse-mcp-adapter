#!/bin/sh
# Power the VM off after MMA_IDLE_HOURS without any activity in the adapter's database (every
# call to the adapter is written there). A stopped cloud VM costs only its disk and address.
# MMA_DATA_DIR, MMA_UPTIME_FILE and MMA_POWEROFF_CMD exist so the logic can be tested.
set -eu
HOURS="${MMA_IDLE_HOURS:?MMA_IDLE_HOURS is not set}"
DATA="${MMA_DATA_DIR:-/opt/muse-adapter/data}"
UPTIME="${MMA_UPTIME_FILE:-/proc/uptime}"
POWEROFF="${MMA_POWEROFF_CMD:-systemctl poweroff}"
LIMIT=$((HOURS * 3600))
NOW=$(date +%s)
UP=$(cut -d. -f1 "$UPTIME")
[ "$UP" -lt "$LIMIT" ] && exit 0   # not up long enough to call it idle
LAST=0
for file in "$DATA/adapter.db" "$DATA/adapter.db-wal"; do
  [ -f "$file" ] || continue
  modified=$(stat -c %Y "$file" 2>/dev/null || stat -f %m "$file")
  [ "$modified" -gt "$LAST" ] && LAST=$modified
done
if [ $((NOW - LAST)) -ge "$LIMIT" ]; then
  logger -t muse-adapter-idle "no activity for ${HOURS}h, powering off"
  $POWEROFF
fi
