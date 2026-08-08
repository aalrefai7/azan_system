#!/bin/bash
# git_sync.sh — pulls the latest adhan_player code from GitHub and restarts
# adhan.service only if something actually changed. Runs on a timer so the
# Pi self-updates without needing SSH access from the dev laptop.

set -e
cd "$(dirname "$0")/.."

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
