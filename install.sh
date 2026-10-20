#!/usr/bin/env bash
set -euo pipefail

NO_PACKAGES=0
if [[ "${1:-}" == "--no-packages" ]]; then
    NO_PACKAGES=1
elif [[ $# -gt 0 ]]; then
    printf 'Usage: %s [--no-packages]\n' "$0" >&2
    exit 2
fi

die() { printf 'gamescope-clipboard-sync: %s\n' "$*" >&2; exit 1; }
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"

as_root() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    elif command -v doas >/dev/null 2>&1; then
        doas "$@"
    else
        die 'Package installation needs root. Install dependencies manually or install sudo/doas.'
    fi
}

dependencies_missing() {
    command -v python3 >/dev/null 2>&1 || return 0
    python3 -c 'import Xlib' >/dev/null 2>&1 || return 0
    command -v xclip >/dev/null 2>&1 || return 0
    command -v wl-paste >/dev/null 2>&1 || return 0
    command -v wl-copy >/dev/null 2>&1 || return 0
    return 1
}

install_dependencies() {
    if command -v pacman >/dev/null 2>&1; then
        as_root pacman -S --needed python python-xlib xclip wl-clipboard
    elif command -v apt-get >/dev/null 2>&1; then
        as_root apt-get install -y python3 python3-xlib xclip wl-clipboard
    elif command -v dnf >/dev/null 2>&1; then
        as_root dnf install -y python3 python3-xlib xclip wl-clipboard
    elif command -v zypper >/dev/null 2>&1; then
        as_root zypper --non-interactive install python3 python3-python-xlib xclip wl-clipboard
    elif command -v apk >/dev/null 2>&1; then
        as_root apk add python3 py3-xlib xclip wl-clipboard
    elif command -v xbps-install >/dev/null 2>&1; then
        as_root xbps-install -Sy python3 python3-xlib xclip wl-clipboard
    elif command -v emerge >/dev/null 2>&1; then
        as_root emerge --ask=n dev-lang/python dev-python/python-xlib x11-misc/xclip gui-apps/wl-clipboard
    else
        die 'Unsupported package manager. Install Python 3, python-xlib, xclip, and wl-clipboard manually, then rerun with --no-packages.'
    fi
}

if dependencies_missing; then
    [[ "$NO_PACKAGES" -eq 0 ]] || die 'Missing dependencies. Install Python 3, python-xlib, xclip, and wl-clipboard first.'
    install_dependencies
fi
dependencies_missing && die 'Dependencies are still missing after package installation.'

[[ -n "${HOME:-}" ]] || die 'HOME is not set.'
[[ -n "${XDG_RUNTIME_DIR:-}" ]] || die 'Run the installer from your graphical desktop session.'

BIN_DIR="${HOME}/.local/bin"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gamescope-clipboard-sync"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
mkdir -p "$BIN_DIR" "$DATA_DIR"
install -m 0755 "$ROOT/gamescope-clipboard-sync.py" "$DATA_DIR/gamescope-clipboard-sync.py"
cat >"$BIN_DIR/gamescope-clipboard-sync" <<EOF
#!/usr/bin/env bash
exec python3 "$DATA_DIR/gamescope-clipboard-sync.py" "\$@"
EOF
chmod 0755 "$BIN_DIR/gamescope-clipboard-sync"

systemd_available=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    systemd_available=1
fi

if [[ "$systemd_available" -eq 1 ]]; then
    mkdir -p "$UNIT_DIR"
    cat >"$UNIT_DIR/gamescope-clipboard-sync.service" <<EOF
[Unit]
Description=Bidirectional Gamescope text clipboard synchronization
After=graphical-session.target

[Service]
Type=simple
ExecStart=$BIN_DIR/gamescope-clipboard-sync
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now gamescope-clipboard-sync.service
    printf 'Installed and started systemd user service.\n'
else
    mkdir -p "$AUTOSTART_DIR"
    escaped_bin="${BIN_DIR//\\/\\\\}"
    escaped_bin="${escaped_bin//\"/\\\"}"
    cat >"$AUTOSTART_DIR/gamescope-clipboard-sync.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Gamescope Clipboard Sync
Comment=Synchronize text clipboard with Gamescope sessions
Exec="$escaped_bin/gamescope-clipboard-sync"
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
    printf 'Installed XDG desktop autostart entry. Log out and back in to start it.\n'
fi

printf 'Installed command: %s/gamescope-clipboard-sync\n' "$BIN_DIR"
