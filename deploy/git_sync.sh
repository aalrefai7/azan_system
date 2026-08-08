#!/bin/bash
# git_sync.sh — pulls the latest adhan_player code from GitHub and restarts
# adhan.service only if something actually changed. Runs on a timer so the
# Pi self-updates without needing SSH access from the dev laptop.

set -e
cd "$(dirname "$0")/.."

# A stale lock (e.g. from a power loss mid-sync — this Pi has a history of
# under-voltage reboots) would otherwise silently break every sync after it.
# Nothing else ever writes to this repo, so it's always safe to clear.
find .git/index.lock -mmin +5 -delete 2>/dev/null || true

BEFORE=$(git rev-parse HEAD)
git fetch --quiet origin
git reset --hard --quiet origin/main
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" != "$AFTER" ]; then
    logger -t adhan-sync "Updated $BEFORE -> $AFTER"
    cp deploy/adhan-sync.service deploy/adhan-sync.timer deploy/bt-volume-guard.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user restart adhan.service
    systemctl --user restart bt-volume-guard.service
fi
