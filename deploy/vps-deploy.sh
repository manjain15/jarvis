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
#   3. Install changed unit files under deploy/systemd/ and daemon-reload
#      when sudo allows it. A denied sudo is logged and does not abort.
#   4. Restart the long-running telegram daemon only if code changed
#      (timer-based jobs pick up new code on their next run automatically)
#
# sudoers for the user that runs this script (usually jarvis). The script
# prints these again when a command is denied:
#   jarvis ALL=(root) NOPASSWD: /usr/bin/systemctl restart jarvis-agent-api.service
#   jarvis ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload
#   jarvis ALL=(root) NOPASSWD: /usr/bin/cp /home/jarvis/jarvis/deploy/systemd/jarvis-*.service /etc/systemd/system/jarvis-*.service, /usr/bin/cp /home/jarvis/jarvis/deploy/systemd/jarvis-*.timer /etc/systemd/system/jarvis-*.timer
# Restart of jarvis-agent-api.service is non-fatal: telegram and spend still
# restart, and a missing sudoers rule cannot abort the deploy halfway.

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

# Copy unit files that differ. sudo failure is logged, not fatal.
install_changed_units() {
    local src="$HOME/jarvis/deploy/systemd"
    local dst="/etc/systemd/system"
    local user unit name
    local changed=0
    user="$(id -un)"
    shopt -s nullglob
    for unit in "$src"/*.service "$src"/*.timer; do
        name="$(basename "$unit")"
        if [[ -f "$dst/$name" ]] && cmp -s "$unit" "$dst/$name"; then
            continue
        fi
        echo "→ Installing systemd unit $name"
        if sudo -n /usr/bin/cp "$unit" "$dst/$name"; then
            changed=1
        else
            echo "⚠️  Could not install $name (sudo cp was denied). Deploy is continuing."
            echo "    Add this sudoers line for ${user}, then copy the unit by hand:"
            echo "    ${user} ALL=(root) NOPASSWD: /usr/bin/cp ${src}/jarvis-*.service ${dst}/jarvis-*.service, /usr/bin/cp ${src}/jarvis-*.timer ${dst}/jarvis-*.timer"
            echo "    sudo /usr/bin/cp ${unit} ${dst}/${name}"
        fi
    done
    shopt -u nullglob
    if [[ "$changed" -eq 1 ]]; then
        if sudo -n /usr/bin/systemctl daemon-reload; then
            echo "→ systemd daemon-reload complete"
        else
            echo "⚠️  systemctl daemon-reload was denied. Deploy is continuing."
            echo "    Add this sudoers line for ${user}, then reload by hand:"
            echo "    ${user} ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload"
            echo "    sudo /usr/bin/systemctl daemon-reload"
        fi
    fi
}

install_changed_units

echo "→ Restarting jarvis-telegram.service"
sudo systemctl restart jarvis-telegram.service

if systemctl is-enabled --quiet jarvis-spend.service 2>/dev/null; then
    echo "→ Restarting jarvis-spend.service"
    sudo systemctl restart jarvis-spend.service
fi

if systemctl is-enabled --quiet jarvis-agent-api.service 2>/dev/null; then
    echo "→ Restarting jarvis-agent-api.service"
    # Non-fatal on purpose. set -e would otherwise stop the deploy here when
    # sudoers does not allow this restart, before the script can finish.
    if ! sudo -n /usr/bin/systemctl restart jarvis-agent-api.service; then
        deploy_user="$(id -un)"
        echo "⚠️  Failed to restart jarvis-agent-api.service. Deploy is continuing."
        echo "    The previous process keeps running until this restart succeeds."
        echo "    Add this sudoers line for ${deploy_user}:"
        echo "    ${deploy_user} ALL=(root) NOPASSWD: /usr/bin/systemctl restart jarvis-agent-api.service"
        echo "    Then: sudo /usr/bin/systemctl restart jarvis-agent-api.service"
    fi
fi

echo "✓ Deploy complete"
