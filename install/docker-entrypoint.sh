#!/bin/sh
# Make /data writable by PUID:PGID (default 1000), then run the app as that user, not root.
set -e
if [ "$(id -u)" = "0" ]; then
  chown -R "$PUID:$PGID" /data
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
