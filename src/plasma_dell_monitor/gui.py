"""PyQt6 GUI: one tab per monitor, live controls, set-then-verify feedback."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from PyQt6.QtCore import QObject, QSize, Qt, QThreadPool, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__, app_settings, calibration, features, input_names, profiles
from .calibration import Range
from .ddcutil_backend import (
    DDCError,
    Monitor,
    VcpReading,
    detect_monitors,
    get_capabilities,
    get_monitor_info,
    get_vcp,
    get_vcp_many,
    get_vcp_word,
    is_read_only,
    run_detect,
    set_vcp,
    set_vcp_bit,
    set_vcp_word,
)
from .workers import Worker

_SETTLE_SECONDS = 0.2  # give the panel a moment before reading a value back

# Quick-adjust enum features exposed in the tray's per-monitor submenu.
_TRAY_QUICK = (features.PRESET_CODE, 0x60, 0xCC, 0xD6)


def app_icon() -> QIcon:
    """A Breeze monitor logo (never the display-config or xwayland icons)."""
    for name in ("monitor", "video-display"):
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    # Fallback: load the Breeze SVG directly if the theme lookup came back empty.
    for path in (
        "/usr/share/icons/breeze/devices/22/monitor.svg",
        "/usr/share/icons/breeze-dark/devices/22/monitor.svg",
    ):
        if os.path.exists(path):
            return QIcon(path)
    return QIcon()


def _blocked_pixmap(size: int = 64):
    """A red 'blocked / not supported' icon as a pixmap (with an emoji fallback)."""
    for name in ("dialog-cancel", "dialog-error", "emblem-error"):
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon.pixmap(size, size)
    return None


def _message_screen(title: str, body: str, blocked: bool = False) -> QWidget:
    """A centered icon + message widget, used for 'no monitors' / 'no Dell' / an
    unsupported-monitor tab."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.addStretch(1)
    pm = _blocked_pixmap(64) if blocked else None
    if pm is not None:
        icon_label = QLabel()
        icon_label.setPixmap(pm)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon_label)
    heading = QLabel(f"<h3>{'⛔ ' if blocked and pm is None else ''}{title}</h3>")
    heading.setTextFormat(Qt.TextFormat.RichText)
    heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(heading)
    text = QLabel(body)
    text.setTextFormat(Qt.TextFormat.RichText)
    text.setWordWrap(True)
    text.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(text)
    lay.addStretch(1)
    return w


# --- status indicator -------------------------------------------------------
class StatusDot(QLabel):
    _STYLES = {
        "idle": ("", ""),
        "busy": ("⟳", "color:#888;"),
        "ok": ("✓", "color:#2e7d32; font-weight:bold;"),
        "warn": ("⚠", "color:#ef6c00; font-weight:bold;"),
        "error": ("✗", "color:#c62828; font-weight:bold;"),
    }

    def __init__(self):
        super().__init__()
        self.setFixedWidth(18)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_state("idle")

    def set_state(self, state: str, tip: str = ""):
        text, style = self._STYLES.get(state, ("", ""))
        self.setText(text)
        self.setStyleSheet(style)
        self.setToolTip(tip)


# --- editable controls ------------------------------------------------------
class _BaseControl(QWidget):
    apply_requested = pyqtSignal(int, int)  # (code, value)

    def __init__(self, code: int):
        super().__init__()
        self.code = code
        self.last_good = 0
        self.lenient = False  # accept a quantised/snapped read-back as success
        self.ok_result = True  # verdict of the most recent apply
        self.status = StatusDot()

    def display_value(self) -> str:
        """Current human-readable value, for the status bar."""
        return self._fmt(self.last_good)

    def set_busy(self):
        self.status.set_state("busy", "applying…")

    def set_error(self, msg: str):
        self._set_silent(self.last_good)
        self.status.set_state("error", msg)

    def revert(self):
        self._set_silent(self.last_good)
        self.status.set_state("idle")

    def load(self, reading: VcpReading):
        """Refresh to a freshly-read value without judging it."""
        self._set_silent(reading.value)
        self.last_good = reading.value
        self.status.set_state("idle")

    def apply_readback(self, reading: VcpReading, requested: int):
        previous = self.last_good
        self._set_silent(reading.value)
        self.last_good = reading.value
        if reading.value == requested:
            self.ok_result = True
            self.status.set_state("ok", f"confirmed = {self._fmt(reading.value)}")
        elif self.lenient and reading.value != previous:
            # Monitor accepted the write but snapped to its nearest supported
            # step (common for sharpness). The change took effect — treat as OK.
            self.ok_result = True
            self.status.set_state(
                "ok", f"applied, snapped to {self._fmt(reading.value)}"
            )
        else:
            self.ok_result = False
            self.status.set_state(
                "warn",
                f"requested {self._fmt(requested)}, monitor reports "
                f"{self._fmt(reading.value)}",
            )

    # subclasses implement:
    def _set_silent(self, value: int):  # pragma: no cover - interface
        raise NotImplementedError

    def _fmt(self, value: int) -> str:
        return str(value)


class ContinuousControl(_BaseControl):
    def __init__(self, code: int, reading: VcpReading, rng: "Range | None" = None):
        super().__init__(code)
        self._syncing = False
        self.last_good = reading.value
        self._apply_bounds(reading, rng)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.spin = QSpinBox()
        self._configure_widgets()
        self._set_silent(reading.value)

        # both widgets stay mirrored (and step-snapped) while dragging/typing…
        self.slider.valueChanged.connect(lambda v: self._sync(v))
        self.spin.valueChanged.connect(lambda v: self._sync(v))
        # …but only push to the monitor once the interaction ends.
        self.slider.sliderReleased.connect(self._emit)
        self.spin.editingFinished.connect(self._emit)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        lay.addWidget(self.status)

    def _apply_bounds(self, reading: VcpReading, rng):
        if rng is not None:
            self.minimum, self.maximum, self.step = rng.minimum, rng.maximum, rng.step
            # calibrated: the value we send is always a valid step, so the
            # read-back should match exactly — no leniency needed.
            self.lenient = False
        else:
            self.minimum, self.maximum, self.step = 0, max(reading.maximum, 1), 1
            # uncalibrated: the panel may clamp/quantise, so accept a snap.
            self.lenient = True

    def _configure_widgets(self):
        for w in (self.slider, self.spin):
            w.setRange(self.minimum, self.maximum)
            w.setSingleStep(self.step)
        span = self.maximum - self.minimum
        self.slider.setPageStep(max(self.step, span // 10 or 1))

    def _snap(self, value: int) -> int:
        value = max(self.minimum, min(self.maximum, value))
        if self.step <= 1:
            return value
        k = round((value - self.minimum) / self.step)
        return max(self.minimum, min(self.maximum, self.minimum + k * self.step))

    def _sync(self, value: int):
        if self._syncing:
            return
        self._syncing = True
        snapped = self._snap(value)
        self.slider.setValue(snapped)
        self.spin.setValue(snapped)
        self._syncing = False

    def _set_silent(self, value: int):
        self._syncing = True
        for w in (self.slider, self.spin):
            w.setValue(max(self.minimum, min(self.maximum, value)))
        self._syncing = False

    def _emit(self):
        self.apply_requested.emit(self.code, self._snap(self.spin.value()))

    def display_value(self) -> str:
        return str(self.spin.value())

    def apply_range(self, rng):
        """Re-bound the widgets after a calibration run."""
        current = self.last_good
        self.minimum, self.maximum, self.step = rng.minimum, rng.maximum, rng.step
        self.lenient = False
        self._configure_widgets()
        self._set_silent(current)


class EnumControl(_BaseControl):
    def __init__(self, code: int, values: list[int], current: int,
                 labels: "dict[int, str] | None" = None):
        super().__init__(code)
        self.last_good = current
        self._labels = dict(labels or {})  # value -> custom display name

        self.combo = QComboBox()
        for v in values:
            self.combo.addItem(self._text(v), v)
        self._set_silent(current)
        self.combo.activated.connect(self._on_activated)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.status)

    def _index_of(self, value: int) -> int:
        for i in range(self.combo.count()):
            if self.combo.itemData(i) == value:
                return i
        return -1

    def _text(self, value: int) -> str:
        custom = self._labels.get(value)
        return custom if custom else features.enum_label(self.code, value)

    def _set_silent(self, value: int):
        idx = self._index_of(value)
        if idx < 0:  # monitor reports a value it didn't advertise — show it anyway
            self.combo.addItem(f"{self._text(value)} (current)", value)
            idx = self.combo.count() - 1
        blocked = self.combo.blockSignals(True)
        self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(blocked)

    def set_labels(self, labels: "dict[int, str] | None"):
        """Replace the custom display labels and refresh the visible item text."""
        self._labels = dict(labels or {})
        for i in range(self.combo.count()):
            self.combo.setItemText(i, self._text(self.combo.itemData(i)))

    def _on_activated(self, index: int):
        self.apply_requested.emit(self.code, self.combo.itemData(index))

    def display_value(self) -> str:
        return self.combo.currentText()

    def _fmt(self, value: int) -> str:
        return self._text(value)


class PresetControl(_BaseControl):
    """Dell-style merged preset: one dropdown that writes 0xDC or 0x14 per item
    and reads its current selection back via the read-only 0xE2 register."""

    def __init__(self, items: list, current_e2: int):
        super().__init__(features.PRESET_CODE)
        self._items = items
        self._pending = None
        self.last_good = current_e2

        self.combo = QComboBox()
        for it in items:
            self.combo.addItem(it.label)
        self._set_silent(current_e2)
        self.combo.activated.connect(self._on_activated)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.status)

    def _index_for_e2(self, e2_value: int) -> int:
        for i, it in enumerate(self._items):
            if it.e2_value == e2_value:
                return i
        return -1

    def _set_silent(self, e2_value: int):
        idx = self._index_for_e2(e2_value)
        blocked = self.combo.blockSignals(True)
        self.combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo.blockSignals(blocked)

    def _on_activated(self, index: int):
        self._pending = self._items[index]
        # write the underlying opcode; verification reads that same opcode back
        self.apply_requested.emit(self._pending.write_code, self._pending.write_value)

    def apply_readback(self, reading: VcpReading, requested: int):
        item = self._pending
        if item is not None and reading.value == requested:
            self.ok_result = True
            self.last_good = item.e2_value
            self._set_silent(item.e2_value)
            self.status.set_state("ok", f"confirmed = {item.label}")
        else:
            self.ok_result = False
            self._set_silent(self.last_good)
            self.status.set_state("warn", "preset did not apply")

    def display_value(self) -> str:
        return self.combo.currentText()

    def _fmt(self, value: int) -> str:
        return next((it.label for it in self._items if it.e2_value == value),
                    f"0x{value:02X}")


class UsbcPriorityControl(_BaseControl):
    """USB-C Prioritization / MST bandwidth (Dell two-level 0xEA / sub-code 0xF8).

    Write-only: the value only reads back meaningfully after a write, so the
    dropdown starts non-committal ("— select —") and each choice writes a 16-bit
    word. Applying it re-negotiates the link, so it's confirmed. On MST/hub
    monitors it takes effect only when daisy-chaining is active.
    """

    def __init__(self, options: list):
        super().__init__(features.USBC_PRIORITY_CODE)
        self._options = options  # list of (word, label)
        self.combo = QComboBox()
        self.combo.addItem("— select —", -1)
        for word, label in options:
            self.combo.addItem(label, word)
        self.combo.activated.connect(self._on_activated)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.status)

    def _on_activated(self, index: int):
        word = self.combo.itemData(index)
        if word is not None and word >= 0:
            self.apply_requested.emit(self.code, word)

    def _set_silent(self, value: int):
        pass  # not readable until written — nothing to restore/display

    def revert(self):
        blocked = self.combo.blockSignals(True)
        self.combo.setCurrentIndex(0)
        self.combo.blockSignals(blocked)
        self.status.set_state("idle")

    def display_value(self) -> str:
        return self.combo.currentText()

    def _fmt(self, value: int) -> str:
        return next((label for word, label in self._options if word == value),
                    f"0x{value:04X}")


class PipModeControl(_BaseControl):
    """PIP/PBP mode selector on the Dell 0xE9 register. Readable (reflects the
    active mode), so it shows current state; apply writes the mode value and
    verifies via read-back (base apply_readback). The command values 0x01/0x02
    (toggle size/position) are separate buttons, not modes, so they never appear
    in this dropdown. Verified working on the P3424WE.
    """

    def __init__(self, modes: list, current: int):
        super().__init__(features.PIP_MODE_CODE)
        self._modes = modes  # list of (value, label)
        self.last_good = current

        self.combo = QComboBox()
        for value, label in modes:
            self.combo.addItem(label, value)
        self._set_silent(current)
        self.combo.activated.connect(self._on_activated)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.status)

    def _index_of(self, value: int) -> int:
        for i in range(self.combo.count()):
            if self.combo.itemData(i) == value:
                return i
        return -1

    def _set_silent(self, value: int):
        idx = self._index_of(value)
        if idx < 0:  # monitor reports a mode it didn't advertise — show it anyway
            self.combo.addItem(f"{self._fmt(value)} (current)", value)
            idx = self.combo.count() - 1
        blocked = self.combo.blockSignals(True)
        self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(blocked)

    def _on_activated(self, index: int):
        self.apply_requested.emit(self.code, self.combo.itemData(index))

    def display_value(self) -> str:
        return self.combo.currentText()

    def _fmt(self, value: int) -> str:
        return next((label for v, label in self._modes if v == value),
                    f"0x{value:02X}")


class MstControl(_BaseControl):
    """MST enable/disable — bit 4 of the Dell 0xEF bitmask register.

    Readable (unlike USB-C Prioritization), so it shows the current state. Its
    apply path is a read-modify-write of bit 4 via MainWindow.apply_mst, behind a
    confirmation (toggling MST reconfigures the DisplayPort topology).
    """

    def __init__(self, on: bool):
        super().__init__(features.MST_CODE)
        self.last_good = 1 if on else 0
        self.combo = QComboBox()
        self.combo.addItem("Disabled", 0)
        self.combo.addItem("Enabled", 1)
        self._set_silent(self.last_good)
        self.combo.activated.connect(self._on_activated)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.status)

    def _set_silent(self, value: int):
        blocked = self.combo.blockSignals(True)
        self.combo.setCurrentIndex(1 if value else 0)
        self.combo.blockSignals(blocked)

    def _on_activated(self, index: int):
        self.apply_requested.emit(self.code, self.combo.itemData(index))

    def load(self, reading: VcpReading):
        # reading.value is the raw 0xEF byte; MST state is bit 4.
        on = bool(reading.value & (1 << features.MST_ENABLE_BIT))
        self.last_good = 1 if on else 0
        self._set_silent(self.last_good)
        self.status.set_state("idle")

    def display_value(self) -> str:
        return self.combo.currentText()

    def _fmt(self, value: int) -> str:
        return "Enabled" if value else "Disabled"


class KvmSwitchControl(_BaseControl):
    """USB-KVM input switch — writes standard Input Source 0x60, but framed as a
    KVM action (the monitor's USB hub follows the active input). Unlike the plain
    Settings input dropdown, it does NOT switch on selection: you pick the target
    computer's input, then press an explicit "Switch" button (switching is
    disruptive — this machine loses the picture if you switch away from it). Reads
    back through 0x60, so it verifies like the normal input control."""

    def __init__(self, values: list[int], current: int,
                 labels: "dict[int, str] | None" = None):
        super().__init__(0x60)
        self.last_good = current
        self._labels = dict(labels or {})

        self.combo = QComboBox()
        for v in values:
            self.combo.addItem(self._text(v), v)
        self._set_silent(current)

        self.switch_btn = QPushButton("Switch")
        self.switch_btn.setToolTip(
            "Switch this monitor's active video input. The keyboard/mouse follow "
            "only if that input's computer is on a different USB upstream.")
        self.switch_btn.clicked.connect(self._on_switch)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.switch_btn)
        lay.addWidget(self.status)

    def _text(self, value: int) -> str:
        custom = self._labels.get(value)
        return custom if custom else features.enum_label(0x60, value)

    def _index_of(self, value: int) -> int:
        for i in range(self.combo.count()):
            if self.combo.itemData(i) == value:
                return i
        return -1

    def _set_silent(self, value: int):
        idx = self._index_of(value)
        if idx < 0:
            self.combo.addItem(f"{self._text(value)} (current)", value)
            idx = self.combo.count() - 1
        blocked = self.combo.blockSignals(True)
        self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(blocked)

    def set_labels(self, labels: "dict[int, str] | None"):
        self._labels = dict(labels or {})
        for i in range(self.combo.count()):
            self.combo.setItemText(i, self._text(self.combo.itemData(i)))

    def _on_switch(self):
        self.apply_requested.emit(self.code, self.combo.currentData())

    def display_value(self) -> str:
        return self.combo.currentText()

    def _fmt(self, value: int) -> str:
        return self._text(value)


class UsbUpstreamControl(_BaseControl):
    """USB-KVM upstream association — the Dell two-level 0xE7 word (0xFF00 Auto,
    0xFF01..0xFF04 = pin USB to computer 1..4). Readable (reads back as 0xFF0N),
    so it shows the current state and verifies; apply writes the word via
    MainWindow.apply_usb_upstream behind a confirmation."""

    def __init__(self, options: list, current_word: int):
        super().__init__(features.USB_KVM_CODE)
        self._options = options  # list of (word, label)
        self.last_good = current_word
        self.combo = QComboBox()
        for word, label in options:
            self.combo.addItem(label, word)
        self._set_silent(current_word)
        self.combo.activated.connect(self._on_activated)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.status)

    def _index_of(self, word: int) -> int:
        for i in range(self.combo.count()):
            if self.combo.itemData(i) == word:
                return i
        return -1

    def _set_silent(self, word: int):
        idx = self._index_of(word)
        if idx < 0:  # monitor reports a slot it didn't advertise — show it anyway
            self.combo.addItem(f"{self._fmt(word)} (current)", word)
            idx = self.combo.count() - 1
        blocked = self.combo.blockSignals(True)
        self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(blocked)

    def _on_activated(self, index: int):
        self.apply_requested.emit(self.code, self.combo.itemData(index))

    def load(self, reading: VcpReading):
        # 0xE7 is a two-level word: reconstruct the full 0xFF0N from raw bytes
        # (the base .load would use only the low byte in reading.value).
        rb = reading.raw_bytes or []
        word = ((rb[-2] << 8) | rb[-1]) if len(rb) >= 2 else reading.value
        self.last_good = word
        self._set_silent(word)
        self.status.set_state("idle")

    def display_value(self) -> str:
        return self.combo.currentText()

    def _fmt(self, word: int) -> str:
        return next((label for w, label in self._options if w == word),
                    f"0x{word:04X}")


class UsbUpstreamPairingControl(QWidget):
    """Per-input USB-upstream pairing for the bit-packed 0xE7 regime (P3424WE class).

    Renders one dropdown per non-USB-C input ("<input>: [USB-C / USB-B / …]"). The
    whole pairing lives in a single 16-bit 0xE7 word (each input = a 2-bit field);
    changing one dropdown does a read-modify-write of that field and writes the full
    word, verified by read-back (via MainWindow.apply_usb_pairing). Not a
    _BaseControl — it drives several fields, so it talks to the window directly and
    carries its own StatusDot. HARDWARE-CONFIRMED encoding (see features)."""

    def __init__(self, panel, window, pairings, upstream_indices, current_word):
        super().__init__()
        self.panel = panel
        self.window_ref = window
        self.current_word = current_word
        self.code = features.USB_KVM_CODE
        self._rows: list[tuple[int, int, QComboBox]] = []  # (input_code, pos, combo)
        self.status = StatusDot()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        for code, pos in pairings:
            combo = QComboBox()
            for idx in upstream_indices:
                combo.addItem(features.usb_upstream_label(idx), idx)
            self._select(combo, features.usb_kvm_field_value(current_word, pos))
            combo.activated.connect(
                lambda _i, c=code, p=pos, cb=combo:
                    window.apply_usb_pairing(panel, self, c, p, cb.currentData())
            )
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(features.enum_label(0x60, code)))
            row.addWidget(combo, 1)
            wrap = QWidget()
            wrap.setLayout(row)
            outer.addWidget(wrap)
            self._rows.append((code, pos, combo))
        srow = QHBoxLayout()
        srow.setContentsMargins(0, 0, 0, 0)
        srow.addStretch(1)
        srow.addWidget(self.status)
        sw = QWidget()
        sw.setLayout(srow)
        outer.addWidget(sw)

    @staticmethod
    def _select(combo: QComboBox, idx: int):
        for i in range(combo.count()):
            if combo.itemData(i) == idx:
                blocked = combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(blocked)
                return

    def set_word(self, word: int):
        """Refresh every dropdown from a (verified) full 0xE7 word."""
        self.current_word = word
        for _code, pos, combo in self._rows:
            self._select(combo, features.usb_kvm_field_value(word, pos))

    def load(self, reading: VcpReading):
        # reconstruct the full 16-bit word from raw bytes (snapshot keeps low byte only)
        rb = reading.raw_bytes or []
        word = ((rb[-2] << 8) | rb[-1]) if len(rb) >= 2 else reading.value
        self.set_word(word)
        self.status.set_state("idle")


# --- rename-inputs dialog ---------------------------------------------------
# --- bulk "copy settings to other monitors" ---------------------------------
# Image settings that mean the same thing on every monitor, so they can be copied
# across panels. Deliberately EXCLUDES per-monitor-inherent settings (input source,
# power, MST, PIP, USB-C priority). The merged Colour Preset is handled separately
# (matched by label, since raw values differ per model).
_BULK_CONTINUOUS: tuple[int, ...] = (0x10, 0x12, 0x87, 0x16, 0x18, 0x1A)


def bulk_eligible_codes(panel: "MonitorPanel") -> list[int]:
    """Codes on this panel that can be offered for bulk copy (continuous image
    settings it actually has, plus the merged Colour Preset)."""
    codes = [c for c in _BULK_CONTINUOUS
             if isinstance(panel.controls.get(c), ContinuousControl)]
    if isinstance(panel.controls.get(features.PRESET_CODE), PresetControl):
        codes.append(features.PRESET_CODE)
    return codes


def plan_bulk_copy(source_panel: "MonitorPanel", target_panel: "MonitorPanel",
                   selected_codes: "set[int]"):
    """Determine what copying source→target would do, restricted to selected_codes.

    Returns ``(writes, skips)`` where:
      * writes = list of ``(code, value, description)`` — direct setvcp writes,
        with continuous values clamped/snapped to the TARGET's calibrated range and
        the Colour Preset resolved to the target's own write code/value.
      * skips  = list of ``(feature_name, reason)`` — eligible on the source but not
        applicable to this target.
    """
    writes: list = []
    skips: list = []
    for code in _BULK_CONTINUOUS:
        if code not in selected_codes:
            continue
        src = source_panel.controls.get(code)
        if not isinstance(src, ContinuousControl):
            continue  # source doesn't have it → not part of the copy set
        tgt = target_panel.controls.get(code)
        if not isinstance(tgt, ContinuousControl):
            skips.append((features.feature_name(code), "not supported"))
            continue
        value = tgt._snap(src.spin.value())  # clamp/snap to the target's range
        writes.append((code, value, f"{features.feature_name(code)} = {value}"))

    if features.PRESET_CODE in selected_codes:
        src_p = source_panel.controls.get(features.PRESET_CODE)
        if isinstance(src_p, PresetControl):
            tgt_p = target_panel.controls.get(features.PRESET_CODE)
            if not isinstance(tgt_p, PresetControl):
                skips.append(("Colour Preset", "not supported"))
            else:
                label = src_p.display_value()
                item = next((it for it in tgt_p._items if it.label == label), None)
                if item is not None:
                    writes.append((item.write_code, item.write_value,
                                   f"Colour Preset = {label}"))
                else:
                    skips.append(("Colour Preset", f"'{label}' not available"))
    return writes, skips


class CopyToMonitorsDialog(QDialog):
    """Pick which settings to copy from one monitor to the others, with a live
    preview of exactly what will be applied and what will be skipped per target."""

    def __init__(self, source_panel, target_panels, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Copy settings to other monitors")
        self.source_panel = source_panel
        self.target_panels = target_panels

        outer = QVBoxLayout(self)
        src_name = source_panel.monitor.model or source_panel.monitor.vendor
        outer.addWidget(QLabel(
            f"Copy image settings from <b>{src_name}</b> to your other Dell monitors."
        ))

        row = QHBoxLayout()
        settings_box = QGroupBox("Settings to copy")
        sv = QVBoxLayout(settings_box)
        self.setting_checks: dict[int, QCheckBox] = {}
        for code in bulk_eligible_codes(source_panel):
            cb = QCheckBox(features.feature_name(code))
            cb.setChecked(True)
            cb.stateChanged.connect(self._update_preview)
            sv.addWidget(cb)
            self.setting_checks[code] = cb
        sv.addStretch(1)
        row.addWidget(settings_box)

        targets_box = QGroupBox("Apply to")
        tv = QVBoxLayout(targets_box)
        self.target_checks: dict = {}
        for p in target_panels:
            cb = QCheckBox(p.monitor.model or p.monitor.vendor)
            cb.setChecked(True)
            cb.stateChanged.connect(self._update_preview)
            tv.addWidget(cb)
            self.target_checks[p] = cb
        tv.addStretch(1)
        row.addWidget(targets_box)
        outer.addLayout(row)

        outer.addWidget(QLabel("Preview:"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(150)
        outer.addWidget(self.preview)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        self.apply_btn = btns.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_btn.clicked.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

        self._update_preview()

    def selected_codes(self) -> "set[int]":
        return {c for c, cb in self.setting_checks.items() if cb.isChecked()}

    def selected_targets(self) -> list:
        return [p for p, cb in self.target_checks.items() if cb.isChecked()]

    def plan(self) -> dict:
        """{target_panel: (writes, skips)} for the current selection."""
        codes = self.selected_codes()
        return {p: plan_bulk_copy(self.source_panel, p, codes)
                for p in self.selected_targets()}

    def _update_preview(self):
        codes = self.selected_codes()
        targets = self.selected_targets()
        lines: list[str] = []
        any_write = False
        if not codes:
            lines.append("No settings selected.")
        elif not targets:
            lines.append("No target monitors selected.")
        else:
            for p in targets:
                writes, skips = plan_bulk_copy(self.source_panel, p, codes)
                label = p.monitor.model or p.monitor.vendor
                if writes:
                    any_write = True
                    lines.append(f"{label}  →  " + ", ".join(d for _, _, d in writes))
                else:
                    lines.append(f"{label}  →  (nothing to apply)")
                for feat, reason in skips:
                    lines.append(f"      skip: {feat} ({reason})")
        self.preview.setPlainText("\n".join(lines))
        self.apply_btn.setEnabled(any_write)


# --- export / import a monitor's settings to/from a JSON file ---------------
# Exportable set = the image settings (same as bulk-copy) + OSD Language. The
# "All settings" import applies all of those the target supports; "Image settings
# only" applies just the image ones. Input, Power, MST/PIP/USB-C are excluded.
SETTINGS_FILE_FORMAT = "plasma-dell-monitor-support/settings"
SETTINGS_FILE_VERSION = 1
_OSD_LANGUAGE_CODE = 0xCC
_EXPORT_CODES: tuple[int, ...] = _BULK_CONTINUOUS + (_OSD_LANGUAGE_CODE,)


def export_settings_dict(panel: "MonitorPanel", codes: "tuple[int, ...]" = _EXPORT_CODES) -> dict:
    """Snapshot a panel's settings as a JSON-serialisable dict keyed by ``0xNN`` /
    ``preset``. Defaults to the full export set (image + OSD Language); pass
    ``_BULK_CONTINUOUS`` for image-only (used by Profiles). The Colour Preset is
    always included when present."""
    settings: dict = {}
    for code in codes:
        ctl = panel.controls.get(code)
        if isinstance(ctl, ContinuousControl):
            settings[f"0x{code:02X}"] = {
                "name": features.feature_name(code), "value": ctl.spin.value()}
        elif isinstance(ctl, EnumControl):
            settings[f"0x{code:02X}"] = {
                "name": features.feature_name(code), "value": ctl.combo.currentData()}
    pc = panel.controls.get(features.PRESET_CODE)
    if isinstance(pc, PresetControl):
        settings["preset"] = {"name": "Colour Preset", "label": pc.display_value()}
    return settings


def plan_import(settings: dict, target_panel: "MonitorPanel", scope: str):
    """Plan applying imported ``settings`` to ``target_panel``.

    ``scope`` is 'all' (image settings + OSD Language) or 'image' (image only).
    Returns ``(writes, skips)`` — writes are ``(code, value, description)``; skips
    are ``(feature_name, reason)``. Settings excluded by ``scope`` (e.g. OSD
    Language when scope='image') are dropped silently, NOT reported as skips —
    only genuinely-unsupported settings become skip warnings."""
    allowed = set(_BULK_CONTINUOUS)
    if scope == "all":
        allowed.add(_OSD_LANGUAGE_CODE)
    writes: list = []
    skips: list = []
    for key, entry in settings.items():
        if key == "preset":
            label = (entry or {}).get("label")
            tgt_p = target_panel.controls.get(features.PRESET_CODE)
            if not isinstance(tgt_p, PresetControl):
                skips.append(("Colour Preset", "is not supported on this monitor"))
            else:
                item = next((it for it in tgt_p._items if it.label == label), None)
                if item is None:
                    skips.append(("Colour Preset",
                                  f"preset '{label}' is not available on this monitor"))
                else:
                    writes.append((item.write_code, item.write_value,
                                   f"Colour Preset = {label}"))
            continue
        try:
            code = int(key, 16)
        except (ValueError, TypeError):
            continue
        if code not in allowed:
            continue  # intentionally out of scope (e.g. OSD Language in image mode)
        value = (entry or {}).get("value")
        if value is None:
            continue
        name = features.feature_name(code)
        ctl = target_panel.controls.get(code)
        if isinstance(ctl, ContinuousControl):
            snapped = ctl._snap(int(value))
            writes.append((code, snapped, f"{name} = {snapped}"))
        elif isinstance(ctl, EnumControl):
            advertised = [ctl.combo.itemData(i) for i in range(ctl.combo.count())]
            if int(value) in advertised:
                writes.append((code, int(value), name))
            else:
                skips.append((name, "value is not available on this monitor"))
        else:
            skips.append((name, "is not supported on this monitor"))
    return writes, skips


class ProfilesBar(QWidget):
    """Per-monitor Profiles: a slot dropdown (0-9, showing "N. Label") plus Save
    and Load buttons. Profiles hold only the visual/image settings. Lives on the
    Settings tab; also driveable via the CLI (`profile load N` / `next` / `prev`)."""

    def __init__(self, panel, window):
        super().__init__()
        self.panel = panel
        self.window = window
        self.combo = QComboBox()
        self.save_btn = QPushButton("Save")
        self.save_btn.setToolTip("Save this monitor's current image settings into "
                                 "the selected profile slot (you can label it).")
        self.load_btn = QPushButton("Load")
        self.load_btn.setToolTip("Apply the selected profile to this monitor.")
        self.save_btn.clicked.connect(self._on_save)
        self.load_btn.clicked.connect(self._on_load)
        self.combo.currentIndexChanged.connect(self._update_load_enabled)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.save_btn)
        lay.addWidget(self.load_btn)
        self.reload()

    def reload(self):
        """Repopulate the dropdown from stored profiles (keeps the selection)."""
        mon = self.panel.monitor
        profs = profiles.load(mon.serial, mon.model)
        keep = self.combo.currentData()
        blocked = self.combo.blockSignals(True)
        self.combo.clear()
        for slot in range(profiles.NUM_SLOTS):
            entry = profs.get(slot)
            label = (entry or {}).get("label", "")
            text = f"{slot}. {label}" if label else f"{slot}. (empty)"
            self.combo.addItem(text, slot)
        if keep is not None:
            self.combo.setCurrentIndex(int(keep))
        self.combo.blockSignals(blocked)
        self._update_load_enabled()

    def _update_load_enabled(self):
        mon = self.panel.monitor
        slot = self.combo.currentData()
        entry = profiles.get_slot(mon.serial, mon.model, slot) if slot is not None else None
        self.load_btn.setEnabled(bool(entry and entry.get("settings")))

    def _on_save(self):
        self.window.save_profile(self.panel, self.combo.currentData(), self)

    def _on_load(self):
        self.window.load_profile(self.panel, self.combo.currentData())


# --- D-Bus control surface (for the CLI / hotkeys) --------------------------
# The running GUI registers this service; `cli.py` is a thin client. Because the
# GUI already has the monitors detected and owns all DDC access, hotkey commands
# apply instantly (no per-press detection) and the GUI's UI updates live.
DBUS_SERVICE = "io.github.plasma_dell_monitor"
DBUS_PATH = "/Control"

# CLI feature name -> continuous VCP code.
CLI_CONTINUOUS: dict[str, int] = {
    "brightness": 0x10, "contrast": 0x12, "sharpness": 0x87,
    "gain-red": 0x16, "gain-green": 0x18, "gain-blue": 0x1A,
}


class _DBusControl(QObject):
    """D-Bus object exposing the CLI surface. Slots are exported via ExportAllSlots;
    they run on the GUI thread, so touching widgets from here is safe."""

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self._window = window

    @pyqtSlot(result=str)
    def ListMonitors(self) -> str:
        return self._window.dbus_list_monitors()

    @pyqtSlot(str, str, str, str, result=str)
    def Adjust(self, target: str, feature: str, action: str, value: str) -> str:
        return self._window.perform_adjust(target, feature, action, value)


class RenameInputsDialog(QDialog):
    """Edit app-side friendly names for a monitor's input-source values."""

    def __init__(self, monitor, values: list[int], current: dict[int, str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rename input sources")
        self._values = values
        self._edits: dict[int, QLineEdit] = {}

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(
            f"Custom names for <b>{monitor.model}</b> inputs "
            f"<span style='color:#888'>· {monitor.short_connector}</span><br>"
            "<span style='color:#888'>Shown in this app only — leave blank to use "
            "the default name.</span>"
        ))
        form = QFormLayout()
        for value in values:
            default = features.enum_label(0x60, value)
            edit = QLineEdit(current.get(value, ""))
            edit.setPlaceholderText(default)
            self._edits[value] = edit
            form.addRow(default + ":", edit)
        outer.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def result_names(self) -> dict[int, str]:
        """Non-empty custom names that differ from the default label."""
        out: dict[int, str] = {}
        for value, edit in self._edits.items():
            name = edit.text().strip()
            if name and name != features.enum_label(0x60, value):
                out[value] = name
        return out


# --- one monitor's panel ----------------------------------------------------
class MonitorPanel(QWidget):
    def __init__(self, monitor: Monitor, caps, values, window: "MainWindow",
                 info: "list[tuple[str, str]] | None" = None):
        super().__init__()
        self.monitor = monitor
        self.window_ref = window
        self.info = info or []
        self.controls: dict[int, _BaseControl] = {}
        self.kvm_switch: "KvmSwitchControl | None" = None  # KVM-tab input switch (0x60)
        self.usb_pairing: "UsbUpstreamPairingControl | None" = None  # KVM-tab 0xE7 pairing
        self.calibration = calibration.load(monitor.serial, monitor.model)
        self.input_names = input_names.load(monitor.serial, monitor.model)

        outer = QVBoxLayout(self)

        header = QLabel(
            f"<b>{monitor.model or 'Unknown'}</b> &nbsp; "
            f"<span style='color:#888'>{monitor.connector} · "
            f"bus {monitor.bus}"
            + (f" · S/N {monitor.serial}" if monitor.serial else "")
            + "</span>"
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(header)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)

        # One QFormLayout per sub-tab (Information / Settings / Color·Picture),
        # each in its own scroll page. Information is leftmost and read-only.
        self.forms: dict[str, QFormLayout] = {}
        self.subtabs = QTabWidget()
        for category in features.TAB_ORDER:
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            self.forms[category] = form
            page = QWidget()
            page_lay = QVBoxLayout(page)
            page_lay.addLayout(form)
            page_lay.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.subtabs.addTab(scroll, category)

        # Information tab — read-only monitor identity + status (no controls).
        info_form = self.forms.get("Information")
        if info_form is not None:
            for label, value in self.info:
                val = QLabel(str(value))
                val.setTextFormat(Qt.TextFormat.PlainText)
                val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                val.setWordWrap(True)
                if label == "Serial number":
                    # value, then a small Copy button immediately after it (a
                    # trailing stretch keeps both left-aligned so the button doesn't
                    # drift right / grow with the window).
                    val.setWordWrap(False)
                    row = QWidget()
                    rl = QHBoxLayout(row)
                    rl.setContentsMargins(0, 0, 0, 0)
                    rl.setSpacing(6)
                    rl.addWidget(val)
                    copy_btn = QToolButton()
                    copy_btn.setAutoRaise(True)  # compact, keeps the row height normal
                    icon = QIcon.fromTheme("edit-copy")
                    if icon.isNull():
                        copy_btn.setText("Copy")
                    else:
                        copy_btn.setIcon(icon)
                    copy_btn.setToolTip("Copy the serial number to the clipboard")
                    copy_btn.clicked.connect(
                        lambda _=False, s=str(value): window.copy_serial(s))
                    # match the plain-label row height so the serial row lines up
                    # evenly with the rows above/below (no taller gap)
                    _h = val.sizeHint().height()
                    copy_btn.setFixedHeight(_h)
                    copy_btn.setIconSize(QSize(_h - 3, _h - 3))
                    rl.addWidget(copy_btn)
                    rl.addStretch(1)
                    info_form.addRow(f"{label}:", row)
                else:
                    info_form.addRow(f"{label}:", val)
            if info_form.rowCount() == 0:
                info_form.addRow(QLabel("<i>No monitor information available.</i>"))
            elif self.info:
                # a plain, natural-sized button (a trailing stretch stops the form
                # from stretching it full-width)
                export_info_btn = QPushButton("Export information…")
                export_info_btn.setToolTip(
                    "Save all the information shown on this tab to a text file.")
                export_info_btn.clicked.connect(lambda: window.export_information(self))
                wrap = QWidget()
                wl = QHBoxLayout(wrap)
                wl.setContentsMargins(0, 0, 0, 0)
                wl.addWidget(export_info_btn)
                wl.addStretch(1)
                info_form.addRow(wrap)

        codes = features.ordered_editable(caps)
        preset_items = features.build_preset_items(caps) if features.has_merged_preset(caps) else []
        if preset_items:
            # place the merged "Colour Preset" right after the image basics
            insert_at = 0
            for i, c in enumerate(codes):
                if c in (0x10, 0x12, 0x87):
                    insert_at = i + 1
            codes.insert(insert_at, features.PRESET_CODE)

        for code in codes:
            if code == features.PRESET_CODE:
                e2_reading = values.get(features.PRESET_CODE)
                current_e2 = e2_reading.value if e2_reading else preset_items[0].e2_value
                ctl: _BaseControl = PresetControl(preset_items, current_e2)
            else:
                reading = values.get(code)
                if reading is None:
                    continue
                kind = features.feature_kind(code, caps.get(code))
                if kind == "continuous":
                    ctl = ContinuousControl(code, reading, self.calibration.get(code))
                elif kind == "enum":
                    labels = self.input_names if code == 0x60 else None
                    ctl = EnumControl(code, caps[code], reading.value, labels)
                else:
                    continue
            ctl.apply_requested.connect(
                lambda code, value, c=ctl: window.apply_setting(self, c, code, value)
            )
            self.controls[code] = ctl
            self.forms[features.feature_category(code)].addRow(
                features.feature_name(code) + ":", ctl
            )

        # Export / Import settings — on the Settings tab (whole-monitor file ops).
        io_row = QHBoxLayout()
        export_btn = QPushButton("Export settings from monitor…")
        export_btn.setToolTip(
            "Save this monitor's image settings and OSD Language to a JSON file."
        )
        export_btn.clicked.connect(lambda: window.export_settings(self))
        import_btn = QPushButton("Import settings to monitor…")
        import_btn.setToolTip(
            "Load a settings JSON file and apply it to this monitor (you choose all "
            "settings or just the image settings)."
        )
        import_btn.clicked.connect(lambda: window.import_settings(self))
        io_row.addWidget(export_btn)
        io_row.addWidget(import_btn)
        io_row.addStretch(1)
        io_wrap = QWidget()
        io_wrap.setLayout(io_row)
        self.forms["Settings"].addRow(io_wrap)

        # Profiles (per-monitor, 10 slots) — visual/image settings only.
        self.profiles_bar = ProfilesBar(self, window)
        self.forms["Settings"].addRow("Profile:", self.profiles_bar)

        # PIP / PBP tab: mode selector + sub-window input + size/position toggles.
        # From DDPM RE (0xE9 mode/command, 0xE8 sub-input, 0xE5 status); verified
        # working on the P3424WE. Gated on 0xE9 advertised.
        pip_form = self.forms["PIP / PBP"]
        if features.has_pip(caps):
            e9_reading = values.get(features.PIP_MODE_CODE)
            current_mode = e9_reading.value if e9_reading else 0x00
            pip = PipModeControl(features.pip_modes(caps), current_mode)
            pip.apply_requested.connect(
                lambda code, value, c=pip: window.apply_pip_mode(self, c, value)
            )
            self.controls[features.PIP_MODE_CODE] = pip
            pip_form.addRow("Mode:", pip)

            # sub-window input source (0xE8), encoded like the main input 0x60
            sub_vals = caps.get(features.PIP_SUBINPUT_CODE)
            if sub_vals:
                sub_reading = values.get(features.PIP_SUBINPUT_CODE)
                sub_current = sub_reading.value if sub_reading else sub_vals[0]
                sub = EnumControl(features.PIP_SUBINPUT_CODE, sub_vals, sub_current,
                                  features.input_labels_for(sub_vals))
                sub.apply_requested.connect(
                    lambda code, value, c=sub: window.apply_setting(self, c, code, value)
                )
                self.controls[features.PIP_SUBINPUT_CODE] = sub
                pip_form.addRow("Sub-window input:", sub)

            # size / position toggles (0xE9 command values 0x01 / 0x02)
            btn_row = QHBoxLayout()
            if features.has_pip_size_toggle(caps):
                bs = QPushButton("Toggle PIP size")
                bs.clicked.connect(
                    lambda _=False: window.apply_pip_command(
                        self, features.PIP_TOGGLE_SIZE, "size")
                )
                btn_row.addWidget(bs)
            if features.has_pip_position_toggle(caps):
                bp = QPushButton("Toggle PIP position")
                bp.clicked.connect(
                    lambda _=False: window.apply_pip_command(
                        self, features.PIP_TOGGLE_POSITION, "position")
                )
                btn_row.addWidget(bp)
            if btn_row.count():
                wrap = QWidget()
                wrap.setLayout(btn_row)
                pip_form.addRow("PIP window:", wrap)

            # read-only status (0xE5), if the monitor returned one
            st = values.get(features.PIP_STATUS_CODE)
            if st is not None:
                pip_form.addRow("Status (0xE5):", QLabel(f"0x{st.value:02X}"))

            note = QLabel(
                "<i>PIP/PBP needs two active inputs to show a second image. "
                "Switching mode briefly blanks the screen while the panel "
                "re-initialises — this is normal.</i>"
            )
            note.setWordWrap(True)
            pip_form.addRow(note)
        else:
            note = QLabel("<i>PIP / PBP is not available on this monitor.</i>")
            note.setWordWrap(True)
            pip_form.addRow(note)

        # MST tab (rightmost): MST enable/disable, with USB-C Prioritization below.
        # The DDC toggle is only offered on "new-spec" 0xEF monitors, where the bit-4
        # write actually controls MST. On "old-spec" monitors (which still advertise
        # 0xEF, e.g. the P2725HE) MST enable is OSD-only — writing 0xEF has no effect
        # (hardware-verified) — so we show an informational note instead of a dead
        # control. Monitors without 0xEF at all show a "not available" note.
        mst_form = self.forms["MST"]
        if features.has_ddc_mst_control(caps):
            ef_reading = values.get(features.MST_CODE)
            mst_on = bool(ef_reading.value & (1 << features.MST_ENABLE_BIT)) if ef_reading else False
            mst = MstControl(mst_on)
            mst.apply_requested.connect(
                lambda code, enable, c=mst: window.apply_mst(self, c, bool(enable))
            )
            self.controls[features.MST_CODE] = mst
            mst_form.addRow("MST (Multi-Stream Transport):", mst)
        elif features.has_mst(caps):
            note = QLabel(
                "<i>MST (Multi-Stream Transport) on this monitor is enabled/disabled "
                "from the monitor's on-screen display (OSD) menu — it is not "
                "controllable over DDC/CI. Once MST is on, the USB-C bandwidth mode "
                "below takes effect.</i>"
            )
            note.setWordWrap(True)
            mst_form.addRow(note)
        else:
            note = QLabel(
                "<i>MST (Multi-Stream Transport) is not available on this monitor.</i>"
            )
            note.setWordWrap(True)
            mst_form.addRow(note)

        # USB-C Prioritization / MST bandwidth (two-level 0xEA) — own apply path,
        # lives on the MST tab, directly below the MST toggle.
        if features.has_usbc_priority(caps):
            usbc = UsbcPriorityControl(features.USBC_PRIORITY_OPTIONS)
            usbc.apply_requested.connect(
                lambda code, word, c=usbc: window.apply_usbc_priority(self, c, word)
            )
            self.controls[features.USBC_PRIORITY_CODE] = usbc
            mst_form.addRow(
                features.feature_name(features.USBC_PRIORITY_CODE) + ":", usbc
            )

        # KVM tab (rightmost): USB-KVM controls, gated on 0xE7 (like MST on 0xEF).
        # A USB-KVM monitor shares one keyboard/mouse (its USB hub) between the PCs
        # on its inputs. Two DDC controls (RE'd from DDPM): the input switch (0x60,
        # the hub follows the active input) and the USB-upstream association (0xE7).
        kvm_form = self.forms["KVM"]
        if features.has_usb_kvm(caps):
            intro = QLabel(
                "<i>This monitor has a built-in USB KVM: it shares one "
                "keyboard/mouse (its USB hub) between the computers connected to "
                "its video inputs.</i>"
            )
            intro.setWordWrap(True)
            kvm_form.addRow(intro)

            # Input switch (0x60) — a distinct KVM-framed control (NOT stored in
            # self.controls[0x60], which is the Settings input dropdown). Pick a
            # target input, press Switch. Its apply path (apply_kvm_switch) also
            # syncs the Settings input control on success.
            input_vals = caps.get(0x60)
            if input_vals:
                in_reading = values.get(0x60)
                in_current = in_reading.value if in_reading else input_vals[0]
                self.kvm_switch = KvmSwitchControl(input_vals, in_current, self.input_names)
                self.kvm_switch.apply_requested.connect(
                    lambda code, value, c=self.kvm_switch:
                        window.apply_kvm_switch(self, c, value)
                )
                kvm_form.addRow("Switch active input:", self.kvm_switch)
                warn = QLabel(
                    "<i>Switches the monitor's active video input. The keyboard/mouse "
                    "follow <b>only</b> if that input's computer is on a different USB "
                    "upstream (see below). If the input isn't this computer, this "
                    "screen switches away — a normal KVM switch-back.</i>"
                )
                warn.setWordWrap(True)
                kvm_form.addRow(warn)

            # USB-upstream association (0xE7). Only offered for the "0xFF0N
            # current-USB" spec (monitor advertises 0xFE) — the encoding we actually
            # decoded. Other 0xE7 monitors (e.g. the P3424WE: 0xE7 reads 0x1400, no
            # 0xFE) use a different, agent-mediated per-input encoding we can't drive
            # safely, so we show a note instead of a control that would write wrong
            # values. See features.usb_kvm_upstream_controllable.
            if features.usb_kvm_upstream_controllable(caps):
                kvm_opts = features.usb_kvm_options(caps)
                # values holds a VcpReading; reconstruct the full 0xFF0N word
                # (the snapshot only keeps the low byte in .value).
                e7_reading = values.get(features.USB_KVM_CODE)
                current_word = features.USB_KVM_AUTO
                if e7_reading is not None:
                    rb = e7_reading.raw_bytes or []
                    current_word = ((rb[-2] << 8) | rb[-1]) if len(rb) >= 2 else e7_reading.value
                usb = UsbUpstreamControl(kvm_opts, current_word)
                usb.apply_requested.connect(
                    lambda code, word, c=usb: window.apply_usb_upstream(self, c, word)
                )
                self.controls[features.USB_KVM_CODE] = usb
                kvm_form.addRow("USB upstream:", usb)
                note = QLabel(
                    "<i>The USB-upstream control is reverse-engineered from Dell's "
                    "software and not yet verified on hardware.</i>"
                )
            elif features.usb_kvm_bitpacked(caps):
                # Per-input USB-upstream pairing (bit-packed 0xE7). Encoding
                # hardware-confirmed on the P3424WE by OSD-correlation.
                pairings = features.usb_kvm_pairings(caps)
                indices = features.usb_kvm_upstream_indices(caps)
                if pairings and len(indices) >= 2:
                    e7_reading = values.get(features.USB_KVM_CODE)
                    current_word = 0
                    if e7_reading is not None:
                        rb = e7_reading.raw_bytes or []
                        current_word = ((rb[-2] << 8) | rb[-1]) if len(rb) >= 2 else e7_reading.value
                    self.usb_pairing = UsbUpstreamPairingControl(
                        self, window, pairings, indices, current_word)
                    # header on its own line, per-input rows below it (not inline)
                    kvm_form.addRow(QLabel("<b>USB upstream (per input):</b>"))
                    kvm_form.addRow(self.usb_pairing)
                note = QLabel(
                    "<i>Choose which USB upstream port (e.g. USB-C or USB-B) feeds "
                    "each video input. Inputs that carry USB themselves (USB-C / "
                    "Thunderbolt) aren't listed. Verified on the P3424WE.</i>"
                )
            else:
                note = QLabel(
                    "<i>The USB hub follows the active input (switch above). "
                    "Per-computer USB pairing, if this monitor has it, is set from "
                    "the monitor's on-screen (OSD) menu.</i>"
                )
            note.setWordWrap(True)
            kvm_form.addRow(note)
        else:
            note = QLabel("<i>USB KVM is not available on this monitor.</i>")
            note.setWordWrap(True)
            kvm_form.addRow(note)

        # Placeholder for any control tab that ended up empty (Information, PIP/PBP,
        # MST and KVM are all populated explicitly above, so skip them here).
        for category, cat_form in self.forms.items():
            if category not in ("Information", "PIP / PBP", "MST", "KVM") and cat_form.rowCount() == 0:
                cat_form.addRow(QLabel("<i>No adjustable features on this tab.</i>"))

        outer.addWidget(self.subtabs, 1)

        buttons = QHBoxLayout()
        refresh = QPushButton("Re-read from monitor")
        refresh.clicked.connect(lambda: window.refresh_panel(self))
        buttons.addWidget(refresh)

        self.calibrate_btn = QPushButton("Calibrate ranges…")
        self.calibrate_btn.setToolTip(
            "Probe each slider's real minimum / maximum / step by briefly writing "
            "test values to the monitor (the screen will flash for a moment). "
            "Result is saved per monitor and reused next time."
        )
        self.calibrate_btn.clicked.connect(lambda: window.calibrate_panel(self))
        has_continuous = any(isinstance(c, ContinuousControl) for c in self.controls.values())
        self.calibrate_btn.setEnabled(has_continuous)
        buttons.addWidget(self.calibrate_btn)

        if 0x60 in self.controls:
            rename_btn = QPushButton("Rename inputs…")
            rename_btn.setToolTip(
                "Give this monitor's inputs friendly names shown in this app "
                "(does not rename them on the monitor itself)."
            )
            rename_btn.clicked.connect(lambda: window.rename_inputs(self))
            buttons.addWidget(rename_btn)

        if features.has_factory_reset(caps):
            reset_btn = QPushButton("Factory reset…")
            reset_btn.setToolTip(
                "Restore ALL of this monitor's settings to factory defaults "
                "(brightness, contrast, colour, input, etc.). Cannot be undone."
            )
            reset_btn.clicked.connect(lambda: window.factory_reset(self))
            buttons.addWidget(reset_btn)

        copy_btn = QPushButton("Copy to other monitors…")
        copy_btn.setToolTip(
            "Copy this monitor's image settings (brightness, contrast, sharpness, "
            "RGB gain, colour preset) to your other Dell monitors — clamped to each "
            "monitor's range and skipping any they don't support."
        )
        copy_btn.clicked.connect(lambda: window.copy_to_monitors(self))
        buttons.addWidget(copy_btn)

        buttons.addStretch(1)
        outer.addLayout(buttons)

    def set_enabled_controls(self, enabled: bool):
        for ctl in self.controls.values():
            ctl.setEnabled(enabled)
        if self.kvm_switch is not None:  # not in self.controls (own 0x60 presentation)
            self.kvm_switch.setEnabled(enabled)
        if self.usb_pairing is not None:  # not in self.controls (own 0xE7 presentation)
            self.usb_pairing.setEnabled(enabled)
        if enabled:
            has_continuous = any(
                isinstance(c, ContinuousControl) for c in self.controls.values()
            )
            self.calibrate_btn.setEnabled(has_continuous)
        else:
            self.calibrate_btn.setEnabled(False)


# --- main window ------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Plasma Dell Monitor Support")
        self.setWindowIcon(app_icon())
        # Start comfortably wide enough that the tab contents don't trigger a
        # horizontal scrollbar; a minimum keeps it usable if the user shrinks it.
        self.resize(820, 700)
        self.setMinimumSize(620, 500)
        self.pool = QThreadPool.globalInstance()
        self.panels: list[MonitorPanel] = []
        self._really_quit = False
        self._tray_hint_shown = False

        self._build_menu_bar()
        self._build_tray()

        self._loading = QWidget()
        lv = QVBoxLayout(self._loading)
        lv.addStretch(1)
        self._loading_label = QLabel("Detecting monitors via ddcutil…")
        self._loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_bar = QProgressBar()
        self._loading_bar.setRange(0, 0)  # indeterminate (busy) spinner
        self._loading_bar.setFixedWidth(240)
        self._retry_btn = QPushButton("Retry detection")
        self._retry_btn.setVisible(False)
        self._retry_btn.clicked.connect(self._retry_detection)
        lv.addWidget(self._loading_label)
        lv.addWidget(self._loading_bar, alignment=Qt.AlignmentFlag.AlignCenter)
        lv.addWidget(self._retry_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        lv.addStretch(1)
        self.setCentralWidget(self._loading)

        self.statusBar().showMessage("Starting…")
        self._register_dbus()  # expose the CLI/hotkey control surface
        self.start_detection()

    # -- menu bar ------------------------------------------------------------
    def _build_menu_bar(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        act_min = QAction("Minimize to &Tray", self)
        act_min.setShortcut("Ctrl+M")
        act_min.triggered.connect(self.hide_to_tray)
        file_menu.addAction(act_min)
        file_menu.addSeparator()
        act_exit = QAction("E&xit", self)
        act_exit.setShortcut("Ctrl+Q")
        act_exit.triggered.connect(self.exit_app)
        file_menu.addAction(act_exit)

        help_menu = bar.addMenu("&Help")
        act_about = QAction("&About", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _show_about(self):
        QMessageBox.about(
            self,
            "About Plasma Dell Monitor Support",
            f"<h3>Plasma Dell Monitor Support</h3>"
            f"<p>Version {__version__}</p>"
            "<p>Control Dell monitor settings over DDC/CI on KDE Plasma, "
            "using <code>ddcutil</code> as the backend.</p>"
            "<p>Adjusts brightness, contrast, sharpness, colour preset, input "
            "source, OSD language and power per monitor, with set-then-verify "
            "read-back and optional per-monitor range calibration.</p>"
            "<p style='color:#888'>HDR / VRR / resolution are handled by KDE "
            "Plasma and are intentionally out of scope.</p>",
        )

    # -- system tray ---------------------------------------------------------
    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("Plasma Dell Monitor Support")
        self.tray.activated.connect(self._tray_activated)
        self._rebuild_tray_menu()  # minimal until monitors are detected
        self.tray.show()

    def _tray_activated(self, reason):
        # Single left-click -> open the main window. (A Plasma-style in-tray
        # slider popup isn't feasible for a third-party Wayland client — the
        # compositor won't let a normal app anchor a window to the tray icon —
        # so we simply raise the main window instead.)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_main()

    def show_main(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self):
        if self.tray is not None:
            self.hide()
        else:
            self.showMinimized()

    def _construct_tray_menu(self) -> QMenu:
        menu = QMenu()
        for panel in self.panels:
            sub = menu.addMenu(panel.monitor.tab_label)
            added = False
            for code in _TRAY_QUICK:
                added |= self._add_quick_submenu(sub, panel, code)
            if added:
                sub.addSeparator()
            sub.addAction("Re-read from monitor", lambda _=False, p=panel: self.refresh_panel(p))
            sub.addAction("Calibrate ranges…", lambda _=False, p=panel: self.calibrate_panel(p))
        if self.panels:
            menu.addSeparator()
        menu.addAction("Show / adjust sliders", lambda: self.show_main())
        menu.addAction("Minimize to Tray", self.hide_to_tray)
        menu.addSeparator()
        menu.addAction("Exit application", self.exit_app)
        return menu

    def _rebuild_tray_menu(self):
        if self.tray is None:
            return
        self._tray_menu = self._construct_tray_menu()  # keep a reference alive
        self.tray.setContextMenu(self._tray_menu)

    def _add_quick_submenu(self, parent: QMenu, panel: "MonitorPanel", code: int) -> bool:
        ctl = panel.controls.get(code)
        if ctl is None:
            return False
        sub = parent.addMenu(features.feature_name(code))
        group = QActionGroup(sub)
        group.setExclusive(True)
        if isinstance(ctl, PresetControl):
            for item in ctl._items:
                act = sub.addAction(item.label)
                act.setCheckable(True)
                act.setChecked(item.e2_value == ctl.last_good)
                group.addAction(act)
                act.triggered.connect(
                    lambda _=False, p=panel, c=ctl, it=item: self._tray_apply_preset(p, c, it)
                )
        elif isinstance(ctl, EnumControl):
            for i in range(ctl.combo.count()):
                value = ctl.combo.itemData(i)
                act = sub.addAction(ctl.combo.itemText(i))
                act.setCheckable(True)
                act.setChecked(value == ctl.last_good)
                group.addAction(act)
                act.triggered.connect(
                    lambda _=False, p=panel, c=ctl, v=value: self.apply_setting(p, c, c.code, v)
                )
        else:
            return False
        return True

    def _tray_apply_preset(self, panel, control: "PresetControl", item):
        control._pending = item  # so apply_readback can resolve the selection
        self.apply_setting(panel, control, item.write_code, item.write_value)

    # -- window close / exit -------------------------------------------------
    def closeEvent(self, event):
        if self._really_quit or self.tray is None:
            # No tray to hide into (or a real exit) — quit the app, since
            # quitOnLastWindowClosed is disabled for the minimise-to-tray flow.
            event.accept()
            QApplication.instance().quit()
            return
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(
                "Still running",
                "Plasma Dell Monitor Support is still in the system tray. "
                "Use ‘Exit application’ from its menu to quit.",
                app_icon(),
                4000,
            )

    def exit_app(self):
        reply = QMessageBox.question(
            self,
            "Exit application",
            "Are you sure you want to exit Plasma Dell Monitor Support?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._really_quit = True
            if self.tray is not None:
                self.tray.hide()
            QApplication.instance().quit()

    # -- detection / snapshot ------------------------------------------------
    def start_detection(self):
        worker = Worker(self._collect_snapshot)
        worker.signals.finished.connect(self._on_detected)
        worker.signals.error.connect(self._on_detect_error)
        self.pool.start(worker)

    def _retry_detection(self):
        """Re-run detection after a failure or an empty result (e.g. once i2c-dev
        is loaded, DDC/CI is enabled in the OSD, or a Dell is plugged in), without
        restarting the app. Restores the loading screen in case a blocked/message
        screen replaced it."""
        self.setCentralWidget(self._loading)
        self._retry_btn.setVisible(False)
        self._loading_bar.setVisible(True)
        self._loading_label.setText("Detecting monitors via ddcutil…")
        self.statusBar().showMessage("Re-detecting…")
        self.start_detection()

    @staticmethod
    def _collect_snapshot():
        snapshots = []
        detect_text = run_detect()  # one shared full `detect` for all Info tabs
        for mon in detect_monitors():
            if not mon.is_dell:
                # Not a Dell panel — don't probe it; it gets an "unsupported" tab.
                snapshots.append((mon, None, {}, None))
                continue
            caps = get_capabilities(mon.bus)
            # Read every value we need in ONE ddcutil call — far faster than one
            # getvcp per code, and fewer bus transactions (gentler on flaky panels).
            codes = [c for c in features.ordered_editable(caps) if not is_read_only(c)]
            if features.has_merged_preset(caps) and 0xE2 in caps:
                codes.append(features.PRESET_CODE)          # 0xE2, seeds merged preset
            if features.has_ddc_mst_control(caps):
                codes.append(features.MST_CODE)             # 0xEF, new-spec MST toggle
            if features.has_pip(caps):
                codes += [features.PIP_MODE_CODE, features.PIP_SUBINPUT_CODE,
                          features.PIP_STATUS_CODE]         # 0xE9 / 0xE8 / 0xE5
            if features.has_usb_kvm(caps):
                codes.append(features.USB_KVM_CODE)         # 0xE7, USB-KVM upstream
            seen: set[int] = set()
            codes = [c for c in codes if not (c in seen or seen.add(c))]  # dedupe, keep order
            values: dict[int, VcpReading] = get_vcp_many(mon.bus, codes)
            info = get_monitor_info(mon, detect_text)  # reuse the shared detect
            snapshots.append((mon, caps, values, info))
        return snapshots

    def _on_detected(self, snapshots):
        if not snapshots:
            self._loading_label.setText(
                "No supported monitors found.\n\n"
                "Only directly-attached DisplayPort / HDMI / USB-C DP-Alt panels "
                "are shown.\nCheck that you have permission to use ddcutil "
                "(i2c-dev / group access)."
            )
            self._loading_bar.setVisible(False)
            self._retry_btn.setVisible(True)
            self.statusBar().showMessage("No monitors detected.")
            return

        dell = [s for s in snapshots if s[0].is_dell]
        non_dell = [s for s in snapshots if not s[0].is_dell]

        # No Dell monitor at all -> blocked screen, nothing to control.
        if not dell:
            vendors = ", ".join(sorted({s[0].vendor for s in non_dell})) or "these"
            self.setCentralWidget(_message_screen(
                "Unsupported monitor(s)",
                "This application only supports <b>Dell</b> monitors.<br><br>"
                f"Detected: {vendors}. No Dell monitor was found.",
                blocked=True,
            ))
            self._rebuild_tray_menu()  # will be minimal (no Dell panels)
            self.statusBar().showMessage("No Dell monitor found — unsupported.")
            return

        # At least one Dell: build normal tabs for Dell, an unsupported tab each
        # for the rest. Only Dell panels go into self.panels (tray / controls).
        tabs = QTabWidget()
        for mon, caps, values, info in dell:
            panel = MonitorPanel(mon, caps, values, self, info)
            self.panels.append(panel)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(panel)
            tabs.addTab(scroll, mon.tab_label)
        for mon, _caps, _values, _info in non_dell:
            tab = _message_screen(
                f"{mon.model or mon.vendor} — not supported",
                f"<b>{mon.vendor}</b> monitor on {mon.short_connector} "
                "is not a Dell panel.<br><br>"
                "This application only controls Dell monitors, so it has been "
                "left untouched.",
                blocked=True,
            )
            tabs.addTab(tab, f"{mon.model or mon.vendor} · unsupported")
        self.setCentralWidget(tabs)
        self._rebuild_tray_menu()

        msg = f"{len(dell)} Dell monitor(s) ready."
        if non_dell:
            msg += f" {len(non_dell)} non-Dell monitor(s) unsupported."
        self.statusBar().showMessage(msg, 6000)

    def _on_detect_error(self, msg: str):
        self.setCentralWidget(self._loading)  # in case a blocked screen replaced it
        self._loading_label.setText(f"Detection failed:\n\n{msg}")
        self._loading_bar.setVisible(False)
        self._retry_btn.setVisible(True)
        self.statusBar().showMessage("Detection failed.")

    # -- applying a setting --------------------------------------------------
    def apply_setting(self, panel: MonitorPanel, control: _BaseControl,
                      code: int, value: int):
        if code in features.CONFIRM_CODES and not self._confirm(code, value):
            control.revert()
            return

        control.set_busy()
        self.statusBar().showMessage(
            f"{panel.monitor.model}: setting {features.feature_name(code)}…"
        )
        bus = panel.monitor.bus
        worker = Worker(self._set_and_verify, bus, code, value)
        worker.signals.finished.connect(
            lambda reading: self._on_applied(panel, control, code, value, reading)
        )
        worker.signals.error.connect(
            lambda msg: self._on_apply_error(panel, control, code, msg)
        )
        self.pool.start(worker)

    @staticmethod
    def _set_and_verify(bus: int, code: int, value: int) -> VcpReading:
        set_vcp(bus, code, value)
        time.sleep(_SETTLE_SECONDS)
        return get_vcp(bus, code)  # read straight back from the hardware

    def _on_applied(self, panel, control, code, requested, reading):
        control.apply_readback(reading, requested)
        if isinstance(control, (EnumControl, PresetControl)):
            self._rebuild_tray_menu()  # refresh tray check-marks
        name = features.feature_name(control.code)
        if control.ok_result:
            self.statusBar().showMessage(
                f"{panel.monitor.model}: {name} = {control.display_value()} ✓", 4000
            )
        else:
            self.statusBar().showMessage(
                f"{panel.monitor.model}: {name} did not take — monitor reports "
                f"{control.display_value()}",
                8000,
            )

    def _on_apply_error(self, panel, control, code, msg):
        control.set_error(msg)
        self.statusBar().showMessage(
            f"{panel.monitor.model}: {features.feature_name(code)} failed — {msg}",
            8000,
        )

    def _confirm(self, code: int, value: int) -> bool:
        name = features.feature_name(code)
        label = features.enum_label(code, value)
        if code == 0x60:
            text = (
                f"Switch <b>{name}</b> to <b>{label}</b>?<br><br>"
                "This changes the monitor's active input; the picture may switch "
                "away from this machine."
            )
        else:  # 0xD6 power
            text = (
                f"Set <b>{name}</b> to <b>{label}</b>?<br><br>"
                "This can put the display into standby or turn it off."
            )
        reply = QMessageBox.question(
            self,
            "Confirm change",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    # -- rename inputs (app-side labels) -------------------------------------
    def rename_inputs(self, panel: MonitorPanel):
        ctl = panel.controls.get(0x60)
        if not isinstance(ctl, EnumControl):
            return
        values = [ctl.combo.itemData(i) for i in range(ctl.combo.count())]
        dlg = RenameInputsDialog(panel.monitor, values, panel.input_names, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        names = dlg.result_names()
        input_names.save(panel.monitor.serial, panel.monitor.model, names)
        panel.input_names = names
        ctl.set_labels(names)          # refresh the dropdown text
        if panel.kvm_switch is not None:
            panel.kvm_switch.set_labels(names)  # and the KVM-tab switch
        self._rebuild_tray_menu()      # and the tray submenu
        self.statusBar().showMessage(f"{panel.monitor.model}: input names updated.", 4000)

    # -- USB-C Prioritization / MST bandwidth (two-level, write-only) --------
    def apply_usbc_priority(self, panel: MonitorPanel, control, word: int):
        label = control._fmt(word)
        reply = QMessageBox.question(
            self,
            "USB-C Prioritization",
            f"Set USB-C Prioritization to <b>{label}</b> on "
            f"<b>{panel.monitor.model}</b>?<br><br>"
            "This re-negotiates the link — the screen may briefly blank and USB "
            "devices may reconnect. On MST/daisy-chain setups it only takes "
            "effect when MST is active.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            control.revert()
            return
        control.set_busy()
        self.statusBar().showMessage(
            f"{panel.monitor.model}: setting USB-C Prioritization…"
        )
        worker = Worker(set_vcp_word, panel.monitor.bus,
                        features.USBC_PRIORITY_CODE, word)
        worker.signals.finished.connect(
            lambda _r: self._on_usbc_applied(panel, control, label)
        )
        worker.signals.error.connect(
            lambda msg: self._on_apply_error(panel, control, features.USBC_PRIORITY_CODE, msg)
        )
        self.pool.start(worker)

    def _on_usbc_applied(self, panel, control, label):
        control.ok_result = True
        control.status.set_state("ok", f"sent: {label} (write-only, not verified)")
        self.statusBar().showMessage(
            f"{panel.monitor.model}: USB-C Prioritization → {label} (sent)", 5000
        )

    # -- MST enable/disable (read-modify-write of 0xEF bit 4) ----------------
    def apply_mst(self, panel: MonitorPanel, control, enable: bool):
        verb = "Enable" if enable else "Disable"
        reply = QMessageBox.question(
            self,
            "MST (Multi-Stream Transport)",
            f"{verb} MST on <b>{panel.monitor.model}</b>?<br><br>"
            "This reconfigures the DisplayPort topology — the screen may blank "
            "and monitors may re-enumerate. You may need to re-read (or restart "
            "the app) afterwards.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            control.revert()
            return
        control.set_busy()
        self.statusBar().showMessage(f"{panel.monitor.model}: {verb.lower()}ing MST…")
        worker = Worker(set_vcp_bit, panel.monitor.bus,
                        features.MST_CODE, features.MST_ENABLE_BIT, enable)
        worker.signals.finished.connect(
            lambda newval: self._on_mst_applied(panel, control, enable, newval)
        )
        worker.signals.error.connect(
            lambda msg: self._on_apply_error(panel, control, features.MST_CODE, msg)
        )
        self.pool.start(worker)

    def _on_mst_applied(self, panel, control, enable, newval):
        actual_on = bool(newval & (1 << features.MST_ENABLE_BIT))
        control.last_good = 1 if actual_on else 0
        control._set_silent(control.last_good)
        if actual_on == enable:
            control.ok_result = True
            control.status.set_state(
                "ok", f"MST {'enabled' if actual_on else 'disabled'}"
            )
        else:
            control.ok_result = False
            control.status.set_state("warn", "MST change did not take")
        self.statusBar().showMessage(
            f"{panel.monitor.model}: MST → {'Enabled' if actual_on else 'Disabled'}",
            6000,
        )

    # -- USB KVM: input switch (0x60) + USB-upstream association (0xE7) -------
    def apply_kvm_switch(self, panel: MonitorPanel, control, value: int):
        """Switch the monitor's active input from the KVM tab (writes 0x60). The
        USB hub follows the input, so this hands keyboard/mouse to that computer.
        Disruptive — this machine may lose the picture — so it's confirmed."""
        label = features.enum_label(0x60, value)
        reply = QMessageBox.question(
            self,
            "USB KVM — switch",
            f"Switch <b>{panel.monitor.model}</b> to <b>{label}</b>?<br><br>"
            "This changes the monitor's active video input. The keyboard/mouse "
            "follow only if that input's computer is on a different USB upstream. "
            "If the input isn't this computer, this screen will switch away.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            control.revert()
            return
        control.set_busy()
        self.statusBar().showMessage(f"{panel.monitor.model}: switching input (KVM)…")
        worker = Worker(self._set_and_verify, panel.monitor.bus, 0x60, value)
        worker.signals.finished.connect(
            lambda reading: self._on_kvm_switch_applied(panel, control, value, reading)
        )
        worker.signals.error.connect(
            lambda msg: self._on_apply_error(panel, control, 0x60, msg)
        )
        self.pool.start(worker)

    def _on_kvm_switch_applied(self, panel, control, requested, reading):
        control.apply_readback(reading, requested)
        # keep the Settings-tab input dropdown (same 0x60) in sync
        settings_input = panel.controls.get(0x60)
        if isinstance(settings_input, EnumControl):
            settings_input._set_silent(reading.value)
        self._rebuild_tray_menu()
        if control.ok_result:
            self.statusBar().showMessage(
                f"{panel.monitor.model}: switched to {control.display_value()} ✓", 5000)
        else:
            self.statusBar().showMessage(
                f"{panel.monitor.model}: input did not switch — monitor reports "
                f"{control.display_value()}", 8000)

    def apply_usb_upstream(self, panel: MonitorPanel, control, word: int):
        """Set the USB-upstream association (0xE7 two-level word). Auto (0xFF00)
        follows the active input; 0xFF0N pins USB to computer N. Reads back."""
        label = control._fmt(word)
        reply = QMessageBox.question(
            self,
            "USB KVM — USB upstream",
            f"Set USB upstream to <b>{label}</b> on "
            f"<b>{panel.monitor.model}</b>?<br><br>"
            "This changes which computer the monitor's shared USB keyboard/mouse "
            "is connected to. The USB devices may briefly reconnect.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            control.revert()
            return
        control.set_busy()
        self.statusBar().showMessage(f"{panel.monitor.model}: setting USB upstream…")
        worker = Worker(self._set_word_and_verify, panel.monitor.bus,
                        features.USB_KVM_CODE, word)
        worker.signals.finished.connect(
            lambda newword: self._on_usb_upstream_applied(panel, control, word, newword)
        )
        worker.signals.error.connect(
            lambda msg: self._on_apply_error(panel, control, features.USB_KVM_CODE, msg)
        )
        self.pool.start(worker)

    @staticmethod
    def _set_word_and_verify(bus: int, code: int, word: int) -> int:
        set_vcp_word(bus, code, word)
        time.sleep(_SETTLE_SECONDS)
        return get_vcp_word(bus, code)  # full 16-bit read-back

    def _on_usb_upstream_applied(self, panel, control, requested, newword):
        control.last_good = newword
        control._set_silent(newword)
        if newword == requested:
            control.ok_result = True
            control.status.set_state("ok", f"confirmed = {control._fmt(newword)}")
        else:
            control.ok_result = False
            control.status.set_state("warn", "USB upstream did not take")
        self.statusBar().showMessage(
            f"{panel.monitor.model}: USB upstream → {control._fmt(newword)}", 6000)

    # -- USB KVM: per-input USB-upstream pairing (bit-packed 0xE7) ------------
    def apply_usb_pairing(self, panel: MonitorPanel, control, input_code: int,
                          pos: int, upstream_index: int):
        """Bind one input's USB to a given upstream, via a read-modify-write of the
        input's 2-bit field in the 0xE7 word (encoding hardware-confirmed on the
        P3424WE). Verified by read-back of that field."""
        input_label = features.enum_label(0x60, input_code)
        up_label = features.usb_upstream_label(upstream_index)
        reply = QMessageBox.question(
            self,
            "USB KVM — USB upstream",
            f"Feed <b>{input_label}</b> from the <b>{up_label}</b> upstream on "
            f"<b>{panel.monitor.model}</b>?<br><br>"
            "This changes which USB port supplies the keyboard/mouse when that "
            "input is active. USB devices may briefly reconnect.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            control.set_word(control.current_word)  # revert the dropdown
            return
        new_word = features.usb_kvm_set_field(control.current_word, pos, upstream_index)
        control.status.set_state("busy", "applying…")
        self.statusBar().showMessage(
            f"{panel.monitor.model}: setting USB upstream for {input_label}…")
        worker = Worker(self._set_word_and_verify, panel.monitor.bus,
                        features.USB_KVM_CODE, new_word)
        worker.signals.finished.connect(
            lambda got: self._on_usb_pairing_applied(
                panel, control, input_label, pos, upstream_index, got)
        )
        worker.signals.error.connect(
            lambda msg: self._on_usb_pairing_error(panel, control, input_code, msg)
        )
        self.pool.start(worker)

    def _on_usb_pairing_applied(self, panel, control, input_label, pos,
                                requested_index, got_word):
        control.set_word(got_word)
        if features.usb_kvm_field_value(got_word, pos) == requested_index:
            control.status.set_state(
                "ok", f"{input_label} → {features.usb_upstream_label(requested_index)}")
            self.statusBar().showMessage(
                f"{panel.monitor.model}: {input_label} USB → "
                f"{features.usb_upstream_label(requested_index)} ✓", 6000)
        else:
            control.status.set_state("warn", "USB upstream did not take")
            self.statusBar().showMessage(
                f"{panel.monitor.model}: {input_label} USB pairing did not take", 8000)

    def _on_usb_pairing_error(self, panel, control, input_code, msg):
        control.set_word(control.current_word)  # revert to last known-good
        control.status.set_state("error", msg)
        self.statusBar().showMessage(
            f"{panel.monitor.model}: USB pairing failed — {msg}", 8000)

    # -- PIP / PBP (0xE9 mode + 0x01/0x02 command toggles) -------------------
    def apply_pip_mode(self, panel: MonitorPanel, control, value: int):
        # Changing PIP/PBP mode re-lays out the panel — confirm it.
        label = control._fmt(value)
        reply = QMessageBox.question(
            self,
            "PIP / PBP",
            f"Set PIP/PBP mode to <b>{label}</b> on {panel.monitor.model}?<br><br>"
            "This changes the on-screen layout. PIP/PBP only shows a second image "
            "when a second input is actively connected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            control.revert()
            return
        control.set_busy()
        self.statusBar().showMessage(
            f"{panel.monitor.model}: setting PIP/PBP mode… (the screen may blank)"
        )
        worker = Worker(self._set_and_verify_pip, panel.monitor.bus,
                        features.PIP_MODE_CODE, value)
        worker.signals.finished.connect(
            lambda result: self._on_pip_mode_applied(panel, control, value, result)
        )
        # Even a hard error is usually just the panel mid-re-init — don't scream
        # "failed"; treat it as applied-but-unconfirmed.
        worker.signals.error.connect(
            lambda msg: self._on_pip_mode_applied(panel, control, value, (None, False))
        )
        self.pool.start(worker)

    @staticmethod
    def _set_and_verify_pip(bus: int, code: int, value: int):
        """Apply a PIP/PBP mode and confirm it, tolerating the panel blanking and
        re-initialising when PIP/PBP is entered or left (which makes the immediate
        read-back error out or return a transient value). Polls the read-back with
        growing delays, ignoring read errors while the monitor is away. Returns
        ``(reading_or_None, confirmed)``."""
        set_vcp(bus, code, value)
        last = None
        for delay in (1.0, 1.5, 2.0, 2.5, 3.0):
            time.sleep(delay)
            try:
                last = get_vcp(bus, code)
            except DDCError:
                last = None  # monitor still coming back — keep waiting
                continue
            if last.value == value:
                return last, True
        return last, False

    def _on_pip_mode_applied(self, panel, control, requested, result):
        reading, confirmed = result
        if confirmed:
            control.apply_readback(reading, requested)  # marks OK "confirmed"
            self.statusBar().showMessage(
                f"{panel.monitor.model}: PIP/PBP = {control.display_value()} ✓", 4000
            )
        elif reading is not None:
            # got a clean read but it disagrees — a genuine mismatch
            control.apply_readback(reading, requested)  # marks warn
            self.statusBar().showMessage(
                f"{panel.monitor.model}: PIP/PBP did not take — monitor reports "
                f"{control.display_value()}", 8000
            )
        else:
            # never got a clean read (monitor kept re-initialising). The change
            # almost certainly took — reflect the request, but say it's unconfirmed.
            control.last_good = requested
            control._set_silent(requested)
            control.ok_result = True
            control.status.set_state(
                "ok", "applied (monitor re-initialised — could not confirm)"
            )
            self.statusBar().showMessage(
                f"{panel.monitor.model}: PIP/PBP = {control.display_value()} "
                "(applied; not confirmed — panel re-initialised)", 6000
            )

    def apply_pip_command(self, panel: MonitorPanel, cmd_value: int, what: str):
        # 0xE9 command (0x01 toggle size / 0x02 cycle position). Fire-and-forget:
        # 0xE9 reads back the current *mode*, not the command, so we just send it
        # and re-read the mode (tolerantly) to refresh the dropdown.
        self.statusBar().showMessage(f"{panel.monitor.model}: PIP {what} toggle…")
        worker = Worker(self._send_and_read_pip, panel.monitor.bus,
                        features.PIP_MODE_CODE, cmd_value)
        worker.signals.finished.connect(
            lambda result: self._on_pip_command_done(panel, what, result)
        )
        worker.signals.error.connect(
            lambda msg: self._on_pip_command_done(panel, what, (None, False))
        )
        self.pool.start(worker)

    @staticmethod
    def _send_and_read_pip(bus: int, code: int, cmd: int):
        """Send a 0xE9 *command* (toggle size/position) and read the resulting
        mode back. 0xE9 reflects the mode, not the command, so we don't verify
        equality — just return the first mode read that succeeds (tolerating the
        brief blank). Returns ``(reading_or_None, ok)``."""
        set_vcp(bus, code, cmd)
        for delay in (1.0, 1.5, 2.0):
            time.sleep(delay)
            try:
                return get_vcp(bus, code), True
            except DDCError:
                continue
        return None, False

    def _on_pip_command_done(self, panel, what, result):
        reading, _confirmed = result
        ctl = panel.controls.get(features.PIP_MODE_CODE)
        if ctl is not None and reading is not None:
            ctl.load(reading)  # reflect whatever mode 0xE9 now reports
        mode = ctl.display_value() if ctl else "?"
        self.statusBar().showMessage(
            f"{panel.monitor.model}: PIP {what} toggled"
            + (f" (mode now {mode})" if reading is not None else " (mode not re-read)"),
            4000,
        )

    # -- manual refresh ------------------------------------------------------
    def refresh_panel(self, panel: MonitorPanel):
        panel.set_enabled_controls(False)
        self.statusBar().showMessage(f"{panel.monitor.model}: re-reading…")
        bus = panel.monitor.bus
        codes = list(panel.controls.keys())
        if panel.usb_pairing is not None and features.USB_KVM_CODE not in codes:
            codes.append(features.USB_KVM_CODE)  # not in .controls, but needs re-reading
        worker = Worker(self._read_values, bus, codes)
        worker.signals.finished.connect(lambda vals: self._on_refreshed(panel, vals))
        worker.signals.error.connect(
            lambda msg: self._on_apply_error(panel, next(iter(panel.controls.values())),
                                             0, msg)
        )
        self.pool.start(worker)

    @staticmethod
    def _read_values(bus: int, codes: list[int]) -> dict[int, VcpReading]:
        # one batched ddcutil call (see get_vcp_many) — was one getvcp per code
        return get_vcp_many(bus, codes)

    def _on_refreshed(self, panel: MonitorPanel, values: dict[int, VcpReading]):
        for code, reading in values.items():
            if code in panel.controls:
                panel.controls[code].load(reading)
        # keep the KVM-tab input switch (not in .controls) in sync with 0x60
        if panel.kvm_switch is not None and 0x60 in values:
            panel.kvm_switch.load(values[0x60])
        # and the per-input USB-upstream pairing (not in .controls) with 0xE7
        if panel.usb_pairing is not None and features.USB_KVM_CODE in values:
            panel.usb_pairing.load(values[features.USB_KVM_CODE])
        panel.set_enabled_controls(True)
        self._rebuild_tray_menu()
        self.statusBar().showMessage(f"{panel.monitor.model}: re-read complete.", 4000)

    # -- factory reset (standard MCCS 0x04) ----------------------------------
    def factory_reset(self, panel: MonitorPanel):
        reply = QMessageBox.question(
            self,
            "Factory Reset",
            f"Restore <b>{panel.monitor.model or 'this monitor'}</b> to "
            "<b>factory defaults</b>?<br><br>"
            "⚠ This resets <b>all</b> of the monitor's settings (brightness, "
            "contrast, colour, input, etc.) via DDC/CI. It <b>cannot be undone</b>.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        bus = panel.monitor.bus
        panel.set_enabled_controls(False)
        self.statusBar().showMessage(
            f"{panel.monitor.model}: restoring factory defaults…"
        )
        worker = Worker(set_vcp, bus, features.FACTORY_RESET_CODE,
                        features.FACTORY_RESET_VALUE)
        # The monitor re-initialises after a reset; re-read shortly afterwards so
        # every control reflects the restored defaults.
        worker.signals.finished.connect(
            lambda _r: QTimer.singleShot(4000, lambda: self.refresh_panel(panel))
        )
        worker.signals.error.connect(
            lambda msg: (
                panel.set_enabled_controls(True),
                self.statusBar().showMessage(
                    f"{panel.monitor.model}: factory reset failed — {msg}", 8000),
            )
        )
        self.pool.start(worker)

    # -- copy settings to other monitors -------------------------------------
    def copy_to_monitors(self, source_panel: MonitorPanel):
        targets = [p for p in self.panels if p is not source_panel]
        if not targets:
            QMessageBox.information(
                self, "Copy settings",
                "Only one Dell monitor is connected — there's nothing to copy to.")
            return
        dlg = CopyToMonitorsDialog(source_panel, targets, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        plans = dlg.plan()
        # jobs: only targets that actually have something to write
        jobs = [(p.monitor.bus, [(c, v) for c, v, _ in writes])
                for p, (writes, _skips) in plans.items() if writes]
        if not jobs:
            return
        for p in plans:
            p.set_enabled_controls(False)
        self.statusBar().showMessage(f"Copying settings to {len(jobs)} monitor(s)…")
        worker = Worker(self._apply_bulk_all, jobs)
        worker.signals.finished.connect(lambda res: self._on_bulk_applied(plans, res))
        worker.signals.error.connect(
            lambda msg: self._on_bulk_error(plans, msg))
        self.pool.start(worker)

    @staticmethod
    def _apply_bulk_all(jobs):
        """jobs: list of (bus, [(code, value), ...]). Writes each in turn (best-effort,
        set-then-read). Returns list of (bus, applied, failed)."""
        out = []
        for bus, writes in jobs:
            applied = failed = 0
            for code, value in writes:
                try:
                    set_vcp(bus, code, value)
                    time.sleep(_SETTLE_SECONDS)
                    get_vcp(bus, code)  # settle read (best-effort verify)
                    applied += 1
                except DDCError:
                    failed += 1
            out.append((bus, applied, failed))
        return out

    def _on_bulk_applied(self, plans, results, verb="Copied"):
        # re-read every target so its controls reflect the new values
        for p in plans:
            self.refresh_panel(p)
        applied = sum(a for _, a, _ in results)
        failed = sum(f for _, _, f in results)
        skipped = sum(len(skips) for _, (_w, skips) in plans.items())
        target_word = "monitor" if len(results) == 1 else "monitors"
        msg = f"{verb} {applied} setting(s) to {len(results)} {target_word}."
        if skipped:
            msg += f" Skipped {skipped}."
        if failed:
            msg += f" {failed} failed."
        self.statusBar().showMessage(msg, 8000)

    def _on_bulk_error(self, plans, msg):
        for p in plans:
            p.set_enabled_controls(True)
        self.statusBar().showMessage(f"Failed — {msg}", 8000)

    # -- Information tab: copy serial / export info --------------------------
    def copy_serial(self, serial: str):
        QApplication.clipboard().setText(serial)
        self.statusBar().showMessage(f"Serial copied: {serial}", 4000)

    def export_information(self, panel: MonitorPanel):
        if not panel.info:
            return
        mon = panel.monitor
        model = mon.model or "Monitor"
        for token in ("DELL", "Dell", "dell"):
            model = model.replace(token, "")
        model = model.strip() or "Monitor"
        default = f"Dell-{model}-{mon.serial or 'unknown'}.txt"
        default = "".join(c if (c.isalnum() or c in "._-") else "_" for c in default)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export information", default, "Text files (*.txt)")
        if not path:
            return
        if not path.lower().endswith(".txt"):
            path += ".txt"
        lines = ["Plasma Dell Monitor Support — monitor information",
                 f"Exported: {datetime.now().isoformat(timespec='seconds')}", ""]
        lines += [f"{label}: {value}" for label, value in panel.info]
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as e:
            QMessageBox.warning(self, "Export failed", f"Couldn't write the file:\n{e}")
            return
        self.statusBar().showMessage(
            f"Exported information to {os.path.basename(path)}", 6000)

    # -- export / import settings (JSON) -------------------------------------
    def export_settings(self, panel: MonitorPanel):
        settings = export_settings_dict(panel)
        if not settings:
            QMessageBox.information(
                self, "Export settings",
                "This monitor has no exportable settings.")
            return
        mon = panel.monitor
        model = mon.model or "Monitor"
        for token in ("DELL", "Dell", "dell"):
            model = model.replace(token, "")
        model = model.strip() or "Monitor"
        default = f"Dell-{model}-{mon.serial or 'unknown'}.json"
        default = "".join(c if (c.isalnum() or c in "._-") else "_" for c in default)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export settings from monitor", default, "Settings JSON (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        payload = {
            "format": SETTINGS_FILE_FORMAT,
            "version": SETTINGS_FILE_VERSION,
            "app_version": __version__,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "monitor": {"brand": "Dell", "model": mon.model, "serial": mon.serial,
                        "connector": mon.connector},
            "settings": settings,
        }
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "Export failed", f"Couldn't write the file:\n{e}")
            return
        self.statusBar().showMessage(
            f"Exported {len(settings)} setting(s) to {os.path.basename(path)}", 6000)

    def import_settings(self, panel: MonitorPanel):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import settings to monitor", "",
            "Settings JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            settings = data["settings"]
            if not isinstance(settings, dict):
                raise ValueError("file has no settings object")
        except (OSError, ValueError, KeyError, TypeError) as e:
            QMessageBox.warning(
                self, "Import failed", f"Couldn't read a valid settings file:\n{e}")
            return

        # Ask scope (All settings vs image only) — don't apply automatically.
        box = QMessageBox(self)
        box.setWindowTitle("Import settings")
        src = (data.get("monitor") or {}).get("model") or "the file"
        box.setText(
            f"Import settings from <b>{src}</b> to <b>{panel.monitor.model}</b>.<br><br>"
            "Which settings do you want to import?")
        all_btn = box.addButton("All settings", QMessageBox.ButtonRole.AcceptRole)
        img_btn = box.addButton("Image settings only", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is all_btn:
            scope = "all"
        elif clicked is img_btn:
            scope = "image"
        else:
            return

        writes, skips = plan_import(settings, panel, scope)
        if skips and not self._warn_skips(skips):
            return  # user cancelled at a warning
        if not writes:
            QMessageBox.information(
                self, "Import settings",
                "None of the settings in this file apply to this monitor.")
            return
        panel.set_enabled_controls(False)
        self.statusBar().showMessage(f"Importing settings to {panel.monitor.model}…")
        jobs = [(panel.monitor.bus, [(c, v) for c, v, _ in writes])]
        worker = Worker(self._apply_bulk_all, jobs)
        worker.signals.finished.connect(
            lambda res: self._on_bulk_applied({panel: (writes, skips)}, res,
                                              verb="Imported"))
        worker.signals.error.connect(
            lambda msg: self._on_bulk_error({panel: (writes, skips)}, msg))
        self.pool.start(worker)

    def _warn_skips(self, skips) -> bool:
        """Show a warning per skipped setting (sequential), honouring the persistent
        'suppress' preference and an in-dialog 'don't show again' checkbox. Returns
        True to proceed with the import, False to cancel it."""
        if app_settings.get("suppress_import_warnings", False):
            return True
        for feat, reason in skips:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Setting will be skipped")
            box.setText(
                f"<b>Warning:</b> {feat} {reason}.<br><br>This setting will be skipped.")
            cb = QCheckBox("Don't show these warnings again")
            box.setCheckBox(cb)
            box.setStandardButtons(
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            box.button(QMessageBox.StandardButton.Ok).setText("Continue")
            box.button(QMessageBox.StandardButton.Cancel).setText("Cancel import")
            result = box.exec()
            if cb.isChecked():
                app_settings.set("suppress_import_warnings", True)  # persist
            if result == QMessageBox.StandardButton.Cancel:
                return False
            if cb.isChecked():
                break  # stop showing warnings, but proceed with the import
        return True

    # -- profiles (per-monitor, 10 slots of visual settings) -----------------
    def _run_apply(self, panel, writes, skips, verb):
        """Apply planned writes to one panel via a worker (set-then-verify),
        refresh it, and report. Shared by profile load."""
        if not writes:
            return "nothing to apply"
        panel.set_enabled_controls(False)
        jobs = [(panel.monitor.bus, [(c, v) for c, v, _ in writes])]
        worker = Worker(self._apply_bulk_all, jobs)
        worker.signals.finished.connect(
            lambda res: self._on_bulk_applied({panel: (writes, skips)}, res, verb=verb))
        worker.signals.error.connect(
            lambda msg: self._on_bulk_error({panel: (writes, skips)}, msg))
        self.pool.start(worker)
        return f"{len(writes)} setting(s)"

    def save_profile(self, panel: MonitorPanel, slot, bar=None):
        if slot is None:
            return
        mon = panel.monitor
        existing = profiles.get_slot(mon.serial, mon.model, slot) or {}
        label, ok = QInputDialog.getText(
            self, "Save profile",
            f"Label for profile {slot} (shown as \"{slot}. <label>\"):",
            text=existing.get("label", ""))
        if not ok:
            return
        settings = export_settings_dict(panel, _BULK_CONTINUOUS)  # image only
        profiles.save_slot(mon.serial, mon.model, slot, label.strip(), settings)
        if bar is not None:
            bar.reload()
        self.statusBar().showMessage(
            f"{mon.model}: saved profile {slot}"
            + (f" ({label.strip()})" if label.strip() else ""), 5000)

    def _apply_profile(self, panel: MonitorPanel, slot) -> str:
        mon = panel.monitor
        entry = profiles.get_slot(mon.serial, mon.model, slot)
        if not entry or not entry.get("settings"):
            return f"profile {slot} is empty"
        writes, skips = plan_import(entry["settings"], panel, "image")
        panel._current_profile_slot = int(slot)
        self._run_apply(panel, writes, skips, verb="Loaded profile")
        label = entry.get("label") or ""
        return f"profile {slot}" + (f" ({label})" if label else "") + " loaded"

    def load_profile(self, panel: MonitorPanel, slot):
        if slot is None:
            return
        result = self._apply_profile(panel, slot)
        if "empty" in result:
            QMessageBox.information(self, "Load profile",
                                    f"Profile {slot} has nothing saved yet.")
            return
        self.statusBar().showMessage(f"{panel.monitor.model}: {result}", 5000)

    def _cycle_profile(self, panel: MonitorPanel, direction: str) -> str:
        mon = panel.monitor
        profs = profiles.load(mon.serial, mon.model)
        filled = sorted(s for s, e in profs.items() if e.get("settings"))
        if not filled:
            return "no saved profiles"
        cur = getattr(panel, "_current_profile_slot", -1)
        if cur in filled:
            i = filled.index(cur)
            nxt = filled[(i + (1 if direction == "next" else -1)) % len(filled)]
        else:
            nxt = filled[0] if direction == "next" else filled[-1]
        return self._apply_profile(panel, nxt)

    # -- D-Bus / CLI control surface -----------------------------------------
    def _register_dbus(self):
        """Register the D-Bus service so the CLI can drive us. Non-fatal if there's
        no session bus (headless) or the name is already taken."""
        try:
            conn = QDBusConnection.sessionBus()
            if not conn.isConnected():
                return
            self._dbus_obj = _DBusControl(self)
            if conn.registerService(DBUS_SERVICE):
                conn.registerObject(
                    DBUS_PATH, self._dbus_obj,
                    QDBusConnection.RegisterOption.ExportAllSlots)
        except Exception:
            pass  # D-Bus unavailable — the GUI still works, just no CLI bridge

    def dbus_list_monitors(self) -> str:
        """JSON list of the controllable Dell monitors (for `cli.py list`)."""
        mons = [{"model": p.monitor.model, "serial": p.monitor.serial,
                 "bus": p.monitor.bus, "connector": p.monitor.connector}
                for p in self.panels]
        return json.dumps(mons)

    def _resolve_targets(self, target: str) -> "list[MonitorPanel]":
        t = (target or "").strip().lower()
        if t in ("", "all"):
            return list(self.panels)
        out = []
        for p in self.panels:
            mon = p.monitor
            if (t == str(mon.serial).lower()
                    or t == str(mon.bus)
                    or t in (mon.model or "").lower()):
                out.append(p)
        return out

    def perform_adjust(self, target: str, feature: str, action: str, value: str) -> str:
        """Apply a CLI adjustment. Runs on the GUI thread; reuses the normal
        apply path (set-then-verify + live UI). Returns a human-readable result."""
        panels = self._resolve_targets(target)
        if not panels:
            return f"error: no monitor matches '{target}'"
        return "\n".join(self._adjust_one(p, feature, action, value) for p in panels)

    def _adjust_one(self, panel: "MonitorPanel", feature: str, action: str,
                    value: str) -> str:
        model = panel.monitor.model or panel.monitor.serial or "monitor"
        feature = (feature or "").lower()
        action = (action or "").lower()

        if feature in CLI_CONTINUOUS:
            code = CLI_CONTINUOUS[feature]
            ctl = panel.controls.get(code)
            if not isinstance(ctl, ContinuousControl):
                return f"{model}: {feature} not supported"
            cur = ctl.spin.value()
            if action == "set":
                try:
                    new = int(value)
                except (ValueError, TypeError):
                    return f"{model}: '{value}' is not a number"
            elif action in ("up", "down"):
                try:
                    step = int(value) if str(value).strip() else ctl.step
                except (ValueError, TypeError):
                    step = ctl.step
                step = step if step > 0 else 1
                new = cur + step if action == "up" else cur - step
            else:
                return f"{model}: unknown action '{action}' for {feature}"
            new = ctl._snap(new)
            self.apply_setting(panel, ctl, code, new)
            return f"{model}: {feature} = {new}"

        if feature == "preset":
            ctl = panel.controls.get(features.PRESET_CODE)
            if not isinstance(ctl, PresetControl):
                return f"{model}: preset not supported"
            n = ctl.combo.count()
            if n == 0:
                return f"{model}: no presets"
            if action in ("next", "prev"):
                idx = (ctl.combo.currentIndex()
                       + (1 if action == "next" else -1)) % n
            elif action == "set":
                idx = next((i for i in range(n)
                            if ctl.combo.itemText(i).lower() == str(value).lower()), -1)
                if idx < 0:
                    return f"{model}: preset '{value}' not available"
            else:
                return f"{model}: unknown action '{action}' for preset"
            ctl.combo.setCurrentIndex(idx)
            ctl._on_activated(idx)  # emits apply_requested -> apply_setting
            return f"{model}: preset = {ctl.combo.itemText(idx)}"

        if feature == "profile":
            if action == "load":
                try:
                    slot = int(value)
                except (ValueError, TypeError):
                    return f"{model}: '{value}' is not a slot number (0-9)"
                if not (0 <= slot < profiles.NUM_SLOTS):
                    return f"{model}: profile slot must be 0-{profiles.NUM_SLOTS - 1}"
                return f"{model}: {self._apply_profile(panel, slot)}"
            if action in ("next", "prev"):
                return f"{model}: {self._cycle_profile(panel, action)}"
            return f"{model}: unknown action '{action}' for profile"

        return f"{model}: unknown feature '{feature}'"

    # -- calibration ---------------------------------------------------------
    def calibrate_panel(self, panel: MonitorPanel):
        cont = {c: ctl for c, ctl in panel.controls.items()
                if isinstance(ctl, ContinuousControl)}
        if not cont:
            return
        reply = QMessageBox.question(
            self,
            "Calibrate ranges",
            f"Probe the real min/max/step of {len(cont)} slider(s) on "
            f"<b>{panel.monitor.model}</b>?<br><br>"
            "Each control will briefly change to test values, so <b>the screen "
            "will flash</b> for a second or two. Settings are restored afterwards "
            "and the result is saved for next time.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        panel.set_enabled_controls(False)
        panel.calibrate_btn.setEnabled(False)
        self.statusBar().showMessage(f"{panel.monitor.model}: calibrating… (screen may flash)")
        bus = panel.monitor.bus
        maxes = {c: ctl.maximum for c, ctl in cont.items()}
        worker = Worker(self._do_calibrate, bus, maxes)
        worker.signals.finished.connect(lambda ranges: self._on_calibrated(panel, ranges))
        worker.signals.error.connect(lambda msg: self._on_calibrate_error(panel, msg))
        self.pool.start(worker)

    @staticmethod
    def _do_calibrate(bus: int, maxes: dict[int, int]) -> dict[int, Range]:
        return {code: calibration.probe_range(bus, code, mx) for code, mx in maxes.items()}

    def _on_calibrated(self, panel: MonitorPanel, ranges: dict[int, Range]):
        calibration.save(panel.monitor.serial, panel.monitor.model, ranges)
        panel.calibration = ranges
        for code, rng in ranges.items():
            if code in panel.controls:
                panel.controls[code].apply_range(rng)
        panel.set_enabled_controls(True)
        panel.calibrate_btn.setEnabled(True)
        summary = ", ".join(
            f"{features.feature_name(c)} {r.minimum}-{r.maximum}"
            + (f"/{r.step}" if r.step > 1 else "")
            for c, r in ranges.items()
        )
        self.statusBar().showMessage(f"Calibrated: {summary}", 10000)

    def _on_calibrate_error(self, panel: MonitorPanel, msg: str):
        panel.set_enabled_controls(True)
        panel.calibrate_btn.setEnabled(True)
        self.statusBar().showMessage(f"{panel.monitor.model}: calibration failed — {msg}", 8000)
