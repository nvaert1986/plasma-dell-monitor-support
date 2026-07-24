"""PyQt6 GUI: one tab per monitor, live controls, set-then-verify feedback."""

from __future__ import annotations

import os
import time

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__, calibration, features, input_names
from .calibration import Range
from .ddcutil_backend import (
    DDCError,
    Monitor,
    VcpReading,
    detect_monitors,
    get_capabilities,
    get_vcp,
    is_read_only,
    set_vcp,
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


# --- rename-inputs dialog ---------------------------------------------------
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
    def __init__(self, monitor: Monitor, caps, values, window: "MainWindow"):
        super().__init__()
        self.monitor = monitor
        self.window_ref = window
        self.controls: dict[int, _BaseControl] = {}
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

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

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
            form.addRow(features.feature_name(code) + ":", ctl)

        # USB-C Prioritization / MST bandwidth (two-level 0xEA) — own apply path
        if features.has_usbc_priority(caps):
            usbc = UsbcPriorityControl(features.USBC_PRIORITY_OPTIONS)
            usbc.apply_requested.connect(
                lambda code, word, c=usbc: window.apply_usbc_priority(self, c, word)
            )
            self.controls[features.USBC_PRIORITY_CODE] = usbc
            form.addRow(features.feature_name(features.USBC_PRIORITY_CODE) + ":", usbc)

        if not self.controls:
            form.addRow(QLabel("<i>No adjustable DDC/CI features reported.</i>"))

        outer.addLayout(form)
        outer.addStretch(1)

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

        buttons.addStretch(1)
        outer.addLayout(buttons)

    def set_enabled_controls(self, enabled: bool):
        for ctl in self.controls.values():
            ctl.setEnabled(enabled)
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
        self.resize(680, 560)
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
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setFixedWidth(240)
        lv.addWidget(self._loading_label)
        lv.addWidget(bar, alignment=Qt.AlignmentFlag.AlignCenter)
        lv.addStretch(1)
        self.setCentralWidget(self._loading)

        self.statusBar().showMessage("Starting…")
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

    @staticmethod
    def _collect_snapshot():
        snapshots = []
        for mon in detect_monitors():
            if not mon.is_dell:
                # Not a Dell panel — don't probe it; it gets an "unsupported" tab.
                snapshots.append((mon, None, {}))
                continue
            caps = get_capabilities(mon.bus)
            values: dict[int, VcpReading] = {}
            for code in features.ordered_editable(caps):
                if is_read_only(code):
                    continue  # e.g. 0xAA Screen Orientation — reportable, not settable
                try:
                    values[code] = get_vcp(mon.bus, code)
                except DDCError:
                    pass  # feature advertised but unreadable — skip its control
            # read the read-only 0xE2 register too, to seed the merged preset
            if features.has_merged_preset(caps) and 0xE2 in caps:
                try:
                    values[features.PRESET_CODE] = get_vcp(mon.bus, 0xE2)
                except DDCError:
                    pass
            snapshots.append((mon, caps, values))
        return snapshots

    def _on_detected(self, snapshots):
        if not snapshots:
            self._loading_label.setText(
                "No supported monitors found.\n\n"
                "Only directly-attached DisplayPort / HDMI / USB-C DP-Alt panels "
                "are shown.\nCheck that you have permission to use ddcutil "
                "(i2c-dev / group access)."
            )
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
        for mon, caps, values in dell:
            panel = MonitorPanel(mon, caps, values, self)
            self.panels.append(panel)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(panel)
            tabs.addTab(scroll, mon.tab_label)
        for mon, _caps, _values in non_dell:
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
        self._loading_label.setText(f"Detection failed:\n\n{msg}")
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

    # -- manual refresh ------------------------------------------------------
    def refresh_panel(self, panel: MonitorPanel):
        panel.set_enabled_controls(False)
        self.statusBar().showMessage(f"{panel.monitor.model}: re-reading…")
        bus = panel.monitor.bus
        codes = list(panel.controls.keys())
        worker = Worker(self._read_values, bus, codes)
        worker.signals.finished.connect(lambda vals: self._on_refreshed(panel, vals))
        worker.signals.error.connect(
            lambda msg: self._on_apply_error(panel, next(iter(panel.controls.values())),
                                             0, msg)
        )
        self.pool.start(worker)

    @staticmethod
    def _read_values(bus: int, codes: list[int]) -> dict[int, VcpReading]:
        out = {}
        for code in codes:
            try:
                out[code] = get_vcp(bus, code)
            except DDCError:
                pass
        return out

    def _on_refreshed(self, panel: MonitorPanel, values: dict[int, VcpReading]):
        for code, reading in values.items():
            if code in panel.controls:
                panel.controls[code].load(reading)
        panel.set_enabled_controls(True)
        self._rebuild_tray_menu()
        self.statusBar().showMessage(f"{panel.monitor.model}: re-read complete.", 4000)

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
