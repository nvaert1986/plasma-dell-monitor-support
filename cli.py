#!/usr/bin/env python3
"""Command-line control for plasma-dell-monitor-support.

Talks to the *running* GUI app over D-Bus, so hotkeys (KDE Custom Shortcuts) can
adjust monitor settings instantly — the GUI already has the monitors detected and
owns all DDC access, so there's no slow per-press detection and the GUI's UI
updates live. The GUI app must be running.

Examples (bind these to keys in System Settings ▸ Shortcuts ▸ Custom Shortcuts):
    python3 cli.py brightness up   --monitor 3DMZZB4
    python3 cli.py brightness down --monitor 3DMZZB4 --step 5
    python3 cli.py contrast set 50 --all
    python3 cli.py preset next --all --notify
    python3 cli.py profile load 6 --monitor 3DMZZB4   # apply saved profile 6
    python3 cli.py profile next   --monitor 3DMZZB4   # cycle saved profiles
    python3 cli.py list

A monitor must be selected with --monitor SERIAL|MODEL|BUS or --all.
"""

from __future__ import annotations

import argparse
import json
import sys

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtDBus import QDBusConnection, QDBusInterface

DBUS_SERVICE = "io.github.plasma_dell_monitor"
DBUS_PATH = "/Control"

_FEATURES = ("brightness", "contrast", "sharpness",
             "gain-red", "gain-green", "gain-blue", "preset", "profile")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cli.py",
        description="Adjust Dell monitor settings via the running GUI (for hotkeys).")
    ap.add_argument("feature", help="one of: " + ", ".join(_FEATURES) + ", or 'list'")
    ap.add_argument("action", nargs="?", default="",
                    help="up | down | set | next | prev")
    ap.add_argument("value", nargs="?", default="",
                    help="value for 'set' (number, or preset name)")
    ap.add_argument("--monitor", default="",
                    help="target monitor by serial, model, or I2C bus number")
    ap.add_argument("--all", action="store_true", help="target all Dell monitors")
    ap.add_argument("--step", type=int, default=None,
                    help="step size for up/down (defaults to the monitor's own step)")
    ap.add_argument("--notify", action="store_true",
                    help="show a desktop notification with the result")
    return ap


def resolve_value(action: str, value: str, step: int | None) -> str:
    """The single string value sent to the GUI: step for up/down, else the value."""
    if action in ("up", "down"):
        return str(step) if step is not None else ""
    return value


def _notify(summary: str, body: str) -> None:
    bus = QDBusConnection.sessionBus()
    notif = QDBusInterface("org.freedesktop.Notifications",
                           "/org/freedesktop/Notifications",
                           "org.freedesktop.Notifications", bus)
    if notif.isValid():
        notif.call("Notify", "Dell Monitor", 0, "video-display",
                   summary, body, [], {}, 4000)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    QCoreApplication(sys.argv)  # QtDBus needs an application object
    bus = QDBusConnection.sessionBus()
    iface = QDBusInterface(DBUS_SERVICE, DBUS_PATH, "", bus)
    if not iface.isValid():
        print("Plasma Dell Monitor Support isn't running — start the app first.",
              file=sys.stderr)
        return 3

    if args.feature == "list":
        reply = iface.call("ListMonitors")
        if reply.errorName():
            print(f"error: {reply.errorMessage()}", file=sys.stderr)
            return 1
        try:
            mons = json.loads(reply.arguments()[0])
        except (IndexError, ValueError):
            mons = []
        if not mons:
            print("No Dell monitors detected by the app.")
            return 0
        for m in mons:
            print(f"{m.get('model','?')}  serial={m.get('serial','?')}  "
                  f"bus={m.get('bus','?')}  ({m.get('connector','?')})")
        return 0

    target = "all" if args.all else args.monitor
    if not target:
        print("Specify a monitor: --monitor SERIAL|MODEL|BUS, or --all.",
              file=sys.stderr)
        return 2

    value = resolve_value(args.action, args.value, args.step)
    reply = iface.call("Adjust", target, args.feature, args.action, str(value))
    if reply.errorName():
        print(f"error: {reply.errorMessage()}", file=sys.stderr)
        return 1
    result = reply.arguments()[0] if reply.arguments() else ""
    print(result)
    if args.notify and result:
        _notify("Monitor", result)
    # a returned line containing "not supported"/"error"/"unknown" -> non-zero
    low = str(result).lower()
    if any(w in low for w in ("not supported", "not available", "unknown", "error",
                              "is not a number")):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
