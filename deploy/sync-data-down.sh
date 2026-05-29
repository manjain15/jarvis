#!/usr/bin/env bash
# sync-data-down.sh — pull the VPS's data/ directory down to the laptop as a backup.
#
# Since the migration, the daemons run on the VPS and data/ (evening checkins,
# daily summaries, telegram_thread.jsonl, seen/offset state) is generated there.
# The VPS is the authoritative copy; this script pulls it back to the laptop so
# there is a second copy if the VPS is ever lost.
#
# Transport: the same key-based SSH path used for the repo sync (no Tailscale).
# NOTE: no --delete — this is a backup, so files removed on the VPS are kept
# locally rather than mirrored away. Re-running overwrites with the VPS version.
#
# Usage:
#   deploy/sync-data-down.sh            # pull
#   deploy/sync-data-down.sh --dry-run  # show what would transfer, change nothing

set -euo pipefail

VPS="jarvis@162.55.172.98"
SSH_KEY="$HOME/.ssh/id_ed25519"

# Resolve data/ relative to the repo root (this script lives in deploy/).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="$REPO_ROOT/data/"
REMOTE_DIR="jarvis/data/"   # relative to the jarvis user's home on the VPS

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN="--dry-run"
    echo "→ DRY RUN — no files will be transferred"
fi

echo "→ Pulling data/ from $VPS:~/$REMOTE_DIR"
rsync -avz $DRY_RUN \
    --exclude='.DS_Store' \
    -e "ssh -i $SSH_KEY" \
    "$VPS:$REMOTE_DIR" "$LOCAL_DIR"

echo "✓ Done"
