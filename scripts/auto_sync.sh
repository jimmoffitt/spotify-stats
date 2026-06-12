#!/bin/bash
#
# auto_sync.sh — run one incremental Spotify sync. Intended for the launchd
# job (see scripts/com.spotify-stats.sync.plist), but safe to run by hand.
#
# Anchors the working directory at the project root because config.py loads
# .local.env and resolves every data path relative to the CWD — launchd starts
# jobs at "/", so without this cd the sync would not find its config or data.
#
PROJECT_DIR="/Users/jimmoffitt/projects/spotify-stats"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG="$PROJECT_DIR/data/sync.log"

cd "$PROJECT_DIR" || exit 1

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') sync start ==="
    "$PYTHON" run_pipeline.py --sync
    echo "=== $(date '+%Y-%m-%d %H:%M:%S %z') sync end (exit $?) ==="
    echo
} >> "$LOG" 2>&1
