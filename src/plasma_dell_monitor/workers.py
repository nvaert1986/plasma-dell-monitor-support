"""Background worker plumbing.

Every ddcutil call blocks (tens to hundreds of ms, capabilities can take a
second or two), so all I/O runs on a QThreadPool. Widgets are only ever touched
back on the main thread via the queued signals defined here.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot


class WorkerSignals(QObject):
    finished = pyqtSignal(object)   # result of fn(...)
    error = pyqtSignal(str)         # error message


class Worker(QRunnable):
    """Run ``fn(*args, **kwargs)`` off the GUI thread and signal the outcome."""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(result)
