#!/usr/bin/env bash
# vps-deploy.sh — run ON THE VPS by GitHub Actions after a push to main.
#
# This script is the forced command for the github-actions-deploy SSH key
# (see ~/.ssh/authorized_keys on the VPS): that key can only run this script,
# so a leaked key cannot open a shell or run arbitrary commands.
#
# What it does:
#   1. Fast-forward ~/jarvis to origin/main (refuses diverged history)
#   2. Reinstall deps only if requirements-vps.txt changed
#   3. Restart the long-running telegram daemon only if code changed
#      (timer-based jobs pick up new code on their next run automatically)

set -euo pipefail

cd "$HOME/jarvis"

BEFORE="$(git rev-parse HEAD)"
git fetch origin main
git merge --ff-only origin/main
AFTER="$(git rev-parse HEAD)"

if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "✓ Already up to date at $AFTER — nothing to deploy"
    exit 0
fi

echo "→ Deployed $BEFORE → $AFTER"

if ! git diff --quiet "$BEFORE" "$AFTER" -- requirements-vps.txt; then
    echo "→ requirements-vps.txt changed, reinstalling dependencies"
    venv/bin/pip install -r requirements-vps.txt
fi

echo "→ Restarting jarvis-telegram.service"
sudo systemctl restart jarvis-telegram.service

if systemctl is-enabled --quiet jarvis-spend.service 2>/dev/null; then
    echo "→ Restarting jarvis-spend.service"
    sudo systemctl restart jarvis-spend.service
fi

echo "✓ Deploy complete"
