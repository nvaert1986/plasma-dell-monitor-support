"""Application entry point."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from .gui import MainWindow, app_icon


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Plasma Dell Monitor Support")
    app.setApplicationDisplayName("Plasma Dell Monitor Support")
    # app_id for Wayland: matches plasma-dell-monitor-support.desktop so the
    # compositor shows the monitor icon in the task switcher / panel.
    app.setDesktopFileName("plasma-dell-monitor-support")
    app.setWindowIcon(app_icon())
    # keep running when the last window is hidden to the tray
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
