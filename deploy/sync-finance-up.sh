#!/usr/bin/env bash
# sync-finance-up.sh — push St. George finance CSVs from the laptop up to the VPS.
#
# Manav downloads statements onto the laptop and drops them in finance/. The
# VPS daemons (morning_brief, alerts, weekly_review) read finance/*.csv on the
# VPS, so run this after each export to keep VPS finance tracking accurate.
#
# Transport: the same key-based SSH path used for the repo sync (no Tailscale).
# Idempotent: re-running just overwrites; the CSV names are fixed
# (everyday.csv / savings1.csv / investing.csv).
#
# Usage:
#   deploy/sync-finance-up.sh            # push
#   deploy/sync-finance-up.sh --dry-run  # show what would transfer, change nothing

set -euo pipefail

VPS="jarvis@34.63.231.218"
SSH_KEY="$HOME/.ssh/id_ed25519"

# Resolve finance/ relative to the repo root (this script lives in deploy/).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="$REPO_ROOT/finance/"
REMOTE_DIR="jarvis/finance/"   # relative to the jarvis user's home on the VPS

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN="--dry-run"
    echo "→ DRY RUN — no files will be transferred"
fi

echo "→ Pushing finance CSVs to $VPS:~/$REMOTE_DIR"
rsync -avz $DRY_RUN \
    --include='*.csv' --exclude='*' \
    -e "ssh -i $SSH_KEY" \
    "$LOCAL_DIR" "$VPS:$REMOTE_DIR"

echo "✓ Done"
