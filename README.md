# gamescope-clippy

Bidirectional **text** clipboard synchronization between your Linux desktop and games running inside Gamescope. After installation, use the normal copy and paste shortcuts:

- Copy text in a game with `Ctrl+C`, then paste it into a desktop app with `Ctrl+V`.
- Copy text on the desktop with `Ctrl+C`, then paste it into a Gamescope game with `Ctrl+V`.

Images, files, and rich text are not synchronized. Gamescope applications must use Xwayland; native Wayland clients inside Gamescope are not currently handled by this tool.

## How it works

Gamescope runs an isolated Xwayland server for games. This background service finds those servers, publishes the current desktop text as their X11 `CLIPBOARD` and `PRIMARY` selections, and serves paste requests. When an X11 game takes ownership of `CLIPBOARD` after a copy action, the service reads that text and updates the desktop clipboard.

The desktop clipboard backend is selected automatically:

- KDE Plasma: reads and writes through Klipper's session D-Bus service when PyGObject is available.
- Other Wayland desktops: uses `wl-paste` and `wl-copy` from `wl-clipboard`.

## Requirements

- Linux with a Wayland desktop session.
- Gamescope with Xwayland enabled (the usual setup for Proton/Wine and X11 games).
- Python 3, `python-xlib`, `xclip`, and `wl-clipboard`.
- A user service manager. `systemd --user` is used when available; otherwise the installer creates an XDG desktop autostart entry.
- On KDE, the service uses Klipper directly if PyGObject is already installed; otherwise it uses the `wl-clipboard` backend.

The installer supports these package managers: `pacman`, `apt`/`apt-get`, `dnf`, `zypper`, `apk`, `xbps-install`, and `emerge`. NixOS, Guix, and other declarative or uncommon systems should install the dependencies with their native configuration and then run the installer with `--no-packages`.

## Install from a checkout

```sh
./install.sh
```

The installer asks the system package manager to install any missing dependencies, copies the program into `~/.local`, and enables it at login. Package installation may ask for your administrator password.

To skip package installation because you manage packages declaratively:

```sh
./install.sh --no-packages
```

## Install directly with curl

After this project is published, install the latest `main` branch with:

```sh
curl -fsSL https://raw.githubusercontent.com/wildesPepega/gamescope-clippy/main/curl-install.sh | bash
```

The bootstrap downloads the project archive from GitHub and runs the same installer. Review `curl-install.sh` before piping it to a shell if you want to inspect what will run. To install a fork, set `GCS_REPOSITORY=owner/repository` for the shell running the script:

```sh
curl -fsSL https://raw.githubusercontent.com/wildesPepega/gamescope-clippy/main/curl-install.sh | GCS_REPOSITORY=owner/repository bash
```

## Check the service

With systemd:

```sh
systemctl --user status gamescope-clipboard-sync.service
journalctl --user -u gamescope-clipboard-sync.service -f
```

List detected Gamescope Xwayland displays:

```sh
~/.local/bin/gamescope-clipboard-sync --list-displays
```

For a foreground run with debug logging:

```sh
~/.local/bin/gamescope-clipboard-sync --verbose
```

If `wl-paste` cannot connect to the desktop clipboard, start the process from the desktop session so it inherits `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR`. KDE users should check that Klipper is running.

## Uninstall

```sh
./uninstall.sh
```

The uninstaller removes the user service/autostart entry and installed files. It leaves system packages installed.

## Limitations

- Text only; images, file lists, and other MIME types are not copied.
- Xwayland games only. Native Wayland games do not expose the X11 selections this service watches.
- Some games do not implement copy and paste themselves. The tool provides the clipboard data but cannot add text selection or clipboard shortcuts to a game that has no such feature.
- The transfer is local to your desktop session and does not upload clipboard contents anywhere.

## Troubleshooting

1. Run `~/.local/bin/gamescope-clipboard-sync --list-displays` while a Gamescope game is running. At least one display should be listed.
2. Check the user service logs with `journalctl --user -u gamescope-clipboard-sync.service`.
3. Confirm the game uses Xwayland and has a text field that accepts `Ctrl+C` and `Ctrl+V`.
4. Confirm `wl-paste`, `wl-copy`, and `xclip` are installed. On KDE, also confirm Klipper is running.

## License

MIT. See [LICENSE](LICENSE).
