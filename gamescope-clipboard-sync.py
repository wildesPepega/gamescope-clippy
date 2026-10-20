#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gamescope-clipboard-sync

Mirrors the current desktop clipboard into every running gamescope Xwayland
display.  Gamescope does not implement the Wayland data-device protocol, so
XWayland inside gamescope never sees the host clipboard.  This daemon claims
the X11 CLIPBOARD/PRIMARY selections on gamescope's own XWayland display(s)
and serves the host clipboard text there.  Wine/Proton and native X11 games
then get the text with Ctrl+V as usual.

Clipboard source:
  * KDE Plasma: Klipper over D-Bus (default, no extra tools needed)
  * everything else: wl-paste from wl-clipboard (polled)
  * explicit override with --source

Targets:
  * all Xwayland servers started by gamescope (detected via /proc)
  * text targets only (UTF8_STRING/STRING/TEXT/text/plain), with INCR
    support for very large texts
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

try:
    from Xlib import X, Xatom
    from Xlib import display as xdisplay
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "Fehler: python-xlib fehlt (Arch: sudo pacman -S python-xlib)\n"
    )
    raise SystemExit(1) from exc

LOG = logging.getLogger("gamescope-clipboard-sync")

# ---------------------------------------------------------------------------
# X11 constants / helpers
# ---------------------------------------------------------------------------

# Names we advertise in TARGETS.  COMPOUND_TEXT and MULTIPLE are intentionally
# not advertised; every modern toolkit (including Wine) asks for UTF8_STRING.
TARGET_NAMES = (
    "TARGETS",
    "TIMESTAMP",
    "UTF8_STRING",
    "STRING",
    "TEXT",
    "text/plain;charset=utf-8",
    "text/plain",
)

# Largest property we send in one X request.  The X11 request length field is
# 16 bit (in 4-byte units), so ~262 KB is the hard ceiling; stay below it.
DIRECT_LIMIT = 200_000
# Chunk size for INCR transfers (large clipboard contents).
INCR_CHUNK = 64_000
# Re-claim interval after another client stole the selection (seconds).
RECLAIM_BACKOFF = 2.0
# Drop unfinished INCR transfers after this many seconds.
INCR_TIMEOUT = 60.0


def _win_id(win) -> int:
    # get_selection_owner() returns 0 (an int) when the selection is unowned,
    # otherwise a Window object.
    if win is None:
        return 0
    return win.id if hasattr(win, "id") else int(win)


# ---------------------------------------------------------------------------
# Host clipboard sources
# ---------------------------------------------------------------------------


class ClipboardSource:
    """Reads and writes the host clipboard."""

    name = "?"

    def get_text(self) -> Optional[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def set_text(self, text: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class KlipperSource(ClipboardSource):
    """KDE Plasma clipboard via org.kde.klipper (text only, always current)."""

    name = "klipper"

    def __init__(self) -> None:
        try:
            from gi.repository import Gio, GLib
        except ImportError as exc:
            raise RuntimeError(
                "PyGObject fehlt (Arch: sudo pacman -S python-gobject)"
            ) from exc

        self._Gio = Gio
        self._GLib = GLib
        self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if self._conn is None:
            raise RuntimeError("Keine D-Bus-Session gefunden")

        reply = self._conn.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.klipper",)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            1500,
            None,
        )
        if not reply.unpack()[0]:
            raise RuntimeError("Klipper läuft nicht (org.kde.klipper fehlt)")

    def get_text(self) -> Optional[str]:
        try:
            reply = self._conn.call_sync(
                "org.kde.klipper",
                "/klipper",
                "org.kde.klipper.klipper",
                "getClipboardContents",
                None,
                self._GLib.VariantType("(s)"),
                self._Gio.DBusCallFlags.NONE,
                1500,
                None,
            )
            return reply.unpack()[0]
        except Exception as exc:  # D-Bus hiccup; try again next tick
            LOG.debug("Klipper-Abfrage fehlgeschlagen: %s", exc)
            return None

    def set_text(self, text: str) -> None:
        try:
            self._conn.call_sync(
                "org.kde.klipper",
                "/klipper",
                "org.kde.klipper.klipper",
                "setClipboardContents",
                self._GLib.Variant("(s)", (text,)),
                None,
                self._Gio.DBusCallFlags.NONE,
                1500,
                None,
            )
        except Exception as exc:
            raise RuntimeError(f"Klipper konnte Clipboard nicht setzen: {exc}") from exc


def _set_default_wayland_display() -> None:
    """systemd user services often lack WAYLAND_DISPLAY; infer it."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return
    try:
        candidates = sorted(
            name
            for name in os.listdir(runtime)
            if name.startswith("wayland-") and not name.endswith(".lock")
        )
    except OSError:
        return
    if candidates:
        os.environ["WAYLAND_DISPLAY"] = candidates[0]


class WlPasteSource(ClipboardSource):
    """Wayland clipboard via wl-paste (works with any wlroots compositor)."""

    name = "wl-paste"

    def __init__(self) -> None:
        if shutil.which("wl-paste") is None:
            raise RuntimeError(
                "wl-paste fehlt (Arch: sudo pacman -S wl-clipboard)"
            )
        if not os.environ.get("WAYLAND_DISPLAY"):
            _set_default_wayland_display()
        if not os.environ.get("WAYLAND_DISPLAY"):
            raise RuntimeError("WAYLAND_DISPLAY ist nicht gesetzt")

    def get_text(self) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.debug("wl-paste fehlgeschlagen: %s", exc)
            return None

        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", "replace")
        if proc.returncode == 1:
            # No selection / empty clipboard.
            return ""
        LOG.debug(
            "wl-paste Exit %s: %s",
            proc.returncode,
            proc.stderr.decode("utf-8", "replace").strip(),
        )
        return None

    def set_text(self, text: str) -> None:
        try:
            proc = subprocess.run(
                ["wl-copy", "--type", "text/plain;charset=utf-8"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"wl-copy fehlgeschlagen: {exc}") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"wl-copy Exit {proc.returncode}: {detail}")


def make_source(name: str) -> ClipboardSource:
    if name in ("auto", "klipper"):
        try:
            return KlipperSource()
        except Exception as exc:
            if name == "klipper":
                raise RuntimeError(f"Klipper-Quelle nicht verfügbar: {exc}")
            LOG.debug("Klipper nicht verfügbar: %s", exc)

    if name in ("auto", "wl-paste"):
        try:
            return WlPasteSource()
        except Exception as exc:
            if name == "wl-paste":
                raise RuntimeError(f"wl-paste-Quelle nicht verfügbar: {exc}")
            LOG.debug("wl-paste nicht verfügbar: %s", exc)

    raise RuntimeError(
        "Keine Clipboard-Quelle gefunden. Installiere wl-clipboard oder "
        "starte unter KDE mit laufendem Klipper."
    )


# ---------------------------------------------------------------------------
# Shared clipboard state
# ---------------------------------------------------------------------------


class SharedClipboard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text = ""
        self._version = 0

    def set(self, text: str) -> bool:
        with self._lock:
            if text == self._text:
                return False
            self._text = text
            self._version += 1
            return True

    def get(self) -> tuple[str, int]:
        with self._lock:
            return self._text, self._version


# ---------------------------------------------------------------------------
# gamescope Xwayland discovery
# ---------------------------------------------------------------------------


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def find_gamescope_displays() -> dict[str, Optional[str]]:
    """Return {display_name: XAUTHORITY} for all gamescope Xwayland servers.

    gamescope starts Xwayland with -force-xrandr-emulation and
    -noTouchPointerEmulation.  The host compositor's own Xwayland does not use
    those flags, which makes the detection unambiguous even when several
    gamescope instances run at once.
    """
    found: dict[str, Optional[str]] = {}

    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = entry.name
        try:
            comm = _read(f"/proc/{pid}/comm").decode("ascii", "replace").strip()
            if not comm.startswith("Xwayland"):
                continue

            raw = _read(f"/proc/{pid}/cmdline")
            args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
            if not any(
                a in ("-force-xrandr-emulation", "-noTouchPointerEmulation")
                for a in args
            ):
                continue

            display_name = next(
                (a for a in args if re.fullmatch(r":\d+(?:\.\d+)?", a)), None
            )
            if not display_name:
                continue

            authfile = None
            try:
                env_raw = _read(f"/proc/{pid}/environ")
                for item in env_raw.split(b"\0"):
                    if item.startswith(b"XAUTHORITY="):
                        authfile = item.split(b"=", 1)[1].decode(
                            "utf-8", "replace"
                        )
                        break
            except OSError:
                pass

            found[display_name] = authfile
        except (OSError, ValueError):
            continue

    return found


# ---------------------------------------------------------------------------
# X11 selection owner (one per gamescope display)
# ---------------------------------------------------------------------------


class GamescopeClipboard(threading.Thread):
    def __init__(
        self,
        display_name: str,
        authfile: Optional[str],
        clipboard: SharedClipboard,
        on_game_copy,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"clip-{display_name}", daemon=True)
        self.display_name = display_name
        self.authfile = authfile
        self.clipboard = clipboard
        self.on_game_copy = on_game_copy
        self.stop_event = stop_event

        self.d = None
        self.win = None
        self.atoms: dict[str, int] = {}
        self._text_atoms: set[int] = set()
        self._transfers: dict[tuple[int, int], dict] = {}
        self._lost_until = 0.0

    # -- thread ------------------------------------------------------------

    def run(self) -> None:
        # python-xlib reads XAUTHORITY from the environment when connecting.
        if self.authfile:
            os.environ["XAUTHORITY"] = self.authfile

        try:
            self.d = xdisplay.Display(self.display_name)
        except Exception as exc:
            LOG.warning(
                "Kann gamescope-Display %s nicht öffnen: %s",
                self.display_name,
                exc,
            )
            return

        self.d.set_error_handler(self._on_xerror)

        try:
            self._setup()
            LOG.info("Spiegele Clipboard nach gamescope-Display %s", self.display_name)
            last_check = 0.0
            while not self.stop_event.is_set():
                self._pump(0.2)

                now = time.monotonic()
                if now - last_check >= 1.0:
                    last_check = now
                    if now >= self._lost_until:
                        self._ensure_owner()
                self._expire_transfers(now)
        except Exception as exc:
            LOG.warning(
                "Clipboard-Owner für %s beendet: %s", self.display_name, exc
            )
        finally:
            try:
                if self.win is not None:
                    self.win.destroy()
                if self.d is not None:
                    self.d.close()
            except Exception:
                pass

    def _setup(self) -> None:
        self.atoms = {
            name: self.d.intern_atom(name, only_if_exists=False)
            for name in TARGET_NAMES + ("INCR",)
        }
        self.atoms["CLIPBOARD"] = self.d.intern_atom("CLIPBOARD")
        self.atoms["PRIMARY"] = self.d.intern_atom("PRIMARY")
        self._text_atoms = {
            self.atoms[name]
            for name in (
                "UTF8_STRING",
                "STRING",
                "TEXT",
                "text/plain;charset=utf-8",
                "text/plain",
            )
        }

        screen = self.d.screen()
        self.win = screen.root.create_window(
            0,
            0,
            1,
            1,
            0,
            screen.root_depth,
            X.InputOutput,
            X.CopyFromParent,
        )
        self.win.set_wm_name("gamescope-clipboard-sync")
        self.d.flush()
        self._claim(self.atoms["CLIPBOARD"])
        self._claim(self.atoms["PRIMARY"])

    def _claim(self, selection: int) -> None:
        self.win.set_selection_owner(selection, X.CurrentTime)
        self.d.flush()

    def _ensure_owner(self) -> None:
        for name in ("CLIPBOARD", "PRIMARY"):
            atom = self.atoms[name]
            owner = self.d.get_selection_owner(atom)
            if _win_id(owner) != _win_id(self.win):
                self._claim(atom)

    # -- event loop --------------------------------------------------------

    def _pump(self, timeout: float) -> None:
        if not self.d.pending_events():
            try:
                readable, _, _ = select.select([self.d.fileno()], [], [], timeout)
            except (OSError, ValueError):
                raise RuntimeError("X-Verbindung verloren")
            if not readable:
                return
        while self.d.pending_events():
            self._handle(self.d.next_event())

    def _handle(self, event) -> None:
        if event.type == X.SelectionRequest:
            self._selection_request(event)
        elif event.type == X.SelectionClear:
            self._lost_until = time.monotonic() + RECLAIM_BACKOFF
            LOG.debug("Selection auf %s verloren", self.display_name)
            if event.selection == self.atoms.get("CLIPBOARD"):
                self._read_game_clipboard()
        elif event.type == X.PropertyNotify:
            self._incr_chunk(event)

    def _on_xerror(self, error, request):
        # Windows can disappear while a client is pasting (e.g. game exit).
        LOG.debug("X-Fehler auf %s: %s", self.display_name, error)
        return 0

    def _read_game_clipboard(self) -> None:
        """Read a game's newly-owned CLIPBOARD selection and publish it on host."""
        env = os.environ.copy()
        env["DISPLAY"] = self.display_name
        if self.authfile:
            env["XAUTHORITY"] = self.authfile
        try:
            proc = subprocess.run(
                ["xclip", "-selection", "clipboard", "-out"],
                env=env,
                capture_output=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("Kann Text nicht aus %s lesen: %s", self.display_name, exc)
            return
        if proc.returncode != 0:
            LOG.debug(
                "xclip konnte %s nicht lesen: %s",
                self.display_name,
                proc.stderr.decode("utf-8", "replace").strip(),
            )
            return
        text = proc.stdout.decode("utf-8", "replace")
        if text:
            self.clipboard.set(text)
            try:
                self.on_game_copy(text)
                LOG.info("Text aus %s in die Desktop-Zwischenablage kopiert", self.display_name)
            except Exception as exc:
                LOG.warning("Konnte Gamescope-Text nicht auf Host setzen: %s", exc)

    # -- selection requests ------------------------------------------------

    def _selection_request(self, event) -> None:
        prop = event.property if event.property != X.NONE else event.target
        success = True

        try:
            if event.target == self.atoms["TARGETS"]:
                targets = [
                    self.atoms[name]
                    for name in (
                        "TARGETS",
                        "TIMESTAMP",
                        "UTF8_STRING",
                        "STRING",
                        "TEXT",
                        "text/plain;charset=utf-8",
                        "text/plain",
                    )
                ]
                event.requestor.change_property(prop, Xatom.ATOM, 32, targets)

            elif event.target == self.atoms["TIMESTAMP"]:
                event.requestor.change_property(
                    prop, Xatom.INTEGER, 32, [int(event.time) & 0xFFFFFFFF]
                )

            elif event.target in self._text_atoms:
                text = self.clipboard.get()[0]
                payload = self._encode(text, event.target)

                if payload is None:
                    success = False
                elif len(payload) <= DIRECT_LIMIT:
                    event.requestor.change_property(prop, event.target, 8, payload)
                else:
                    success = self._start_incr(event, prop, payload)

            else:
                success = False

            self.d.flush()
        except Exception as exc:
            LOG.debug("SelectionRequest auf %s fehlgeschlagen: %s", self.display_name, exc)
            success = False

        if not success:
            # Refuse the conversion: SelectionNotify with property=None.
            prop = X.NONE

        notify = xdisplay.event.SelectionNotify(
            time=event.time,
            requestor=event.requestor,
            selection=event.selection,
            target=event.target,
            property=prop,
        )
        try:
            event.requestor.send_event(notify)
            self.d.flush()
        except Exception as exc:
            LOG.debug("SelectionNotify fehlgeschlagen: %s", exc)

    def _encode(self, text: str, target: int) -> Optional[bytes]:
        if target == self.atoms["STRING"]:
            try:
                return text.encode("latin-1")
            except UnicodeEncodeError:
                # Not representable as STRING; let the client ask for UTF-8.
                return None
        return text.encode("utf-8")

    # -- INCR (large payloads) ---------------------------------------------

    def _start_incr(self, event, prop: int, payload: bytes) -> bool:
        try:
            # Must be selected before the property is created so the first
            # PropertyDelete from the requestor cannot be missed.
            event.requestor.change_attributes(event_mask=X.PropertyChangeMask)
            event.requestor.change_property(
                prop, self.atoms["INCR"], 32, [len(payload)]
            )
        except Exception as exc:
            LOG.debug("INCR-Start fehlgeschlagen: %s", exc)
            return False

        self._transfers[(_win_id(event.requestor), prop)] = {
            "requestor": event.requestor,
            "property": prop,
            "target": event.target,
            "data": payload,
            "offset": 0,
            "started": time.monotonic(),
        }
        self.d.flush()
        return True

    def _incr_chunk(self, event) -> None:
        if event.state != X.PropertyDelete:
            return

        key = (_win_id(event.window), event.atom)
        transfer = self._transfers.get(key)
        if transfer is None:
            return

        offset = transfer["offset"]
        chunk = transfer["data"][offset : offset + INCR_CHUNK]
        transfer["offset"] = offset + len(chunk)

        try:
            if chunk:
                transfer["requestor"].change_property(
                    transfer["property"], transfer["target"], 8, chunk
                )
            else:
                # Zero-length property terminates the INCR transfer.
                transfer["requestor"].change_property(
                    transfer["property"], transfer["target"], 8, b""
                )
                del self._transfers[key]
            self.d.flush()
        except Exception as exc:
            LOG.debug("INCR-Chunk fehlgeschlagen: %s", exc)
            self._transfers.pop(key, None)

    def _expire_transfers(self, now: float) -> None:
        for key, transfer in list(self._transfers.items()):
            if now - transfer["started"] > INCR_TIMEOUT:
                LOG.debug("INCR-Transfer abgebrochen (Timeout)")
                del self._transfers[key]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronisiert Text in beide Richtungen zwischen Desktop und "
            "gamescope-Xwayland."
        )
    )
    parser.add_argument(
        "--source",
        choices=("auto", "klipper", "wl-paste"),
        default="auto",
        help="Clipboard-Quelle (Standard: auto)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Abfrageintervall der Host-Zwischenablage in Sekunden (Standard: 0.5)",
    )
    parser.add_argument(
        "--list-displays",
        action="store_true",
        help="Gefundene gamescope-Displays ausgeben und beenden",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Debug-Ausgaben aktivieren"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list_displays:
        displays = find_gamescope_displays()
        if not displays:
            print("Keine gamescope-Displays gefunden.")
        for name, authfile in sorted(displays.items()):
            print(f"{name}\tXAUTHORITY={authfile or '-'}")
        return 0

    try:
        source = make_source(args.source)
    except Exception as exc:
        LOG.error("%s", exc)
        return 1

    LOG.info("Clipboard-Quelle: %s", source.name)

    clipboard = SharedClipboard()
    stop_event = threading.Event()
    workers: dict[str, GamescopeClipboard] = {}

    def _shutdown(signum, frame):
        LOG.info("Beende (%s) …", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    def publish_game_copy(text: str) -> None:
        source.set_text(text)

    # Set the initial value immediately so the first paste works even if the
    # clipboard does not change afterwards.
    initial = source.get_text()
    if initial is not None:
        clipboard.set(initial)

    last_discovery = 0.0

    try:
        while not stop_event.is_set():
            text = source.get_text()
            if text is not None and clipboard.set(text):
                LOG.debug("Clipboard geändert (%d Zeichen)", len(text))

            now = time.monotonic()
            if now - last_discovery >= 1.0:
                last_discovery = now
                found = find_gamescope_displays()

                for display_name, authfile in found.items():
                    worker = workers.get(display_name)
                    if worker is None or not worker.is_alive():
                        if worker is not None:
                            worker.join(timeout=0.1)
                        worker = GamescopeClipboard(
                            display_name,
                            authfile,
                            clipboard,
                            publish_game_copy,
                            stop_event,
                        )
                        workers[display_name] = worker
                        worker.start()

                for display_name in list(workers):
                    if display_name not in found:
                        worker = workers.pop(display_name)
                        LOG.debug("gamescope-Display %s verschwunden", display_name)
                        worker.join(timeout=0.1)

            stop_event.wait(max(0.05, args.interval))
    except Exception as exc:
        LOG.error("Unerwarteter Fehler: %s", exc)
        if args.verbose:
            LOG.exception("Details")
        return 2
    finally:
        stop_event.set()
        for worker in workers.values():
            worker.join(timeout=0.5)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
