#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gamescope-clipboard-sync"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now gamescope-clipboard-sync.service 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
fi

rm -f "$CONFIG_DIR/systemd/user/gamescope-clipboard-sync.service"
rm -f "$CONFIG_DIR/autostart/gamescope-clipboard-sync.desktop"
rm -f "$HOME/.local/bin/gamescope-clipboard-sync"
rm -rf "$DATA_DIR"
printf 'Removed Gamescope Clipboard Sync. System packages were left installed.\n'
