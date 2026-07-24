"""Thin wrapper around the ``ddcutil`` CLI.

All monitor I/O goes through here. Functions are plain/synchronous and raise
:class:`DDCError` on failure; the GUI runs them off the main thread (see
``workers.py``). Monitors are always addressed by **I2C bus number** (``--bus``)
because bus numbers are stable across runs, whereas ddcutil "display numbers"
can be reassigned.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field


class DDCError(Exception):
    """Raised when a ddcutil invocation fails or its output can't be parsed."""


# I2C/DDC reads are occasionally flaky; these substrings mark a retryable glitch.
_TRANSIENT = (
    "No monitor detected",
    "DDCRC_NULL_RESPONSE",
    "DDCRC_DDC_DATA",
    "DDCRC_READ_ALL_ZERO",
    "Error reading",
    "retry",
)


# --- monitor connector filtering -------------------------------------------
# We only support directly-attached DisplayPort / HDMI / USB-C DP-Alt panels.
# USB-C DP-Alt monitors enumerate as a normal "DP-n" DRM connector, so matching
# DP + HDMI covers them. Everything else (eDP laptop panels, DSI, LVDS) is out.
_ACCEPT_CONNECTOR = re.compile(r"-(DP|HDMI)-", re.IGNORECASE)
_EXCLUDE_CONNECTOR = re.compile(r"eDP|LVDS|DSI", re.IGNORECASE)


@dataclass
class Monitor:
    display_num: int
    bus: int
    connector: str
    mfg: str
    model: str
    serial: str

    @property
    def label(self) -> str:
        name = self.model or f"Display {self.display_num}"
        return f"{name}" + (f"  ({self.serial})" if self.serial else "")

    @property
    def short_connector(self) -> str:
        """e.g. 'card1-DP-3' -> 'DP-3'."""
        return re.sub(r"^card\d+-", "", self.connector)

    @property
    def is_dell(self) -> bool:
        """True if the EDID says this is a Dell panel ('DEL' PNP mfg id)."""
        return self.mfg.strip().upper() == "DEL" or self.model.strip().upper().startswith("DELL")

    @property
    def vendor(self) -> str:
        """Best-effort human vendor name for messages."""
        return self.mfg.strip() or "Unknown"

    @property
    def tab_label(self) -> str:
        name = self.model or f"Display {self.display_num}"
        return f"{name} · {self.short_connector}"


@dataclass
class VcpReading:
    """Result of a single ``getvcp``."""

    code: int
    kind: str  # 'continuous' | 'simple' | 'complex'
    value: int = 0          # current value (for continuous; sl byte for enums)
    maximum: int = 0        # only meaningful when kind == 'continuous'
    raw_bytes: list = field(default_factory=list)


def _run(args: list[str], timeout: int = 25, retries: int = 1) -> str:
    if shutil.which("ddcutil") is None:
        raise DDCError("ddcutil is not installed or not in PATH")
    try:
        proc = subprocess.run(
            ["ddcutil", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise DDCError(f"ddcutil timed out: {' '.join(args)}")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        if retries > 0 and any(t in msg for t in _TRANSIENT):
            time.sleep(0.25)
            return _run(args, timeout=timeout, retries=retries - 1)
        raise DDCError(msg or f"ddcutil exited with code {proc.returncode}")
    return proc.stdout


# --- detection --------------------------------------------------------------
def detect_monitors() -> list[Monitor]:
    """Return the list of supported, directly-attached monitors.

    Skips "Invalid display" blocks (which is what ddcutil emits for laptop eDP
    panels and many MST/dock situations) and filters by connector type.
    """
    out = _run(["detect", "--terse"])
    monitors: list[Monitor] = []

    for block in re.split(r"\n\s*\n", out):
        block = block.strip("\n")
        if not block:
            continue
        header = block.splitlines()[0].strip()
        if not header.lower().startswith("display"):
            continue  # skip "Invalid display" etc.

        m_num = re.search(r"display\s+(\d+)", header, re.IGNORECASE)
        m_bus = re.search(r"/dev/i2c-(\d+)", block)
        m_con = re.search(r"DRM connector:\s*(\S+)", block)
        m_mon = re.search(r"Monitor:\s*([^\n]*)", block)

        if not (m_num and m_bus and m_con):
            continue

        connector = m_con.group(1)
        if _EXCLUDE_CONNECTOR.search(connector) or not _ACCEPT_CONNECTOR.search(connector):
            continue

        mfg = model = serial = ""
        if m_mon:
            parts = [p.strip() for p in m_mon.group(1).split(":")]
            parts += [""] * (3 - len(parts))
            mfg, model, serial = parts[0], parts[1], parts[2]

        monitors.append(
            Monitor(
                display_num=int(m_num.group(1)),
                bus=int(m_bus.group(1)),
                connector=connector,
                mfg=mfg,
                model=model,
                serial=serial,
            )
        )
    return monitors


# --- capabilities -----------------------------------------------------------
def get_capabilities(bus: int) -> dict[int, list[int] | None]:
    """Parse the monitor's raw VESA capability string.

    Returns ``{vcp_code: [supported_values] | None}`` where ``None`` means the
    feature advertised no value list (continuous or opaque), and ``[]`` means it
    advertised an empty list (e.g. ``63()`` — feature present but no options).
    """
    out = _run(["--bus", str(bus), "capabilities", "--terse"])
    m = re.search(r"Unparsed capabilities string:\s*(.*)", out)
    raw = m.group(1) if m else out

    vcp_body = _extract_paren_group(raw, "vcp")
    if vcp_body is None:
        raise DDCError("could not find vcp(...) section in capability string")

    features: dict[int, list[int] | None] = {}
    for code_hex, vals in re.findall(r"([0-9A-Fa-f]{2})(?:\(([^)]*)\))?", vcp_body):
        code = int(code_hex, 16)
        if vals is None:
            features[code] = None
        else:
            vals = vals.strip()
            features[code] = [int(v, 16) for v in vals.split()] if vals else []
    return features


def _extract_paren_group(text: str, key: str) -> str | None:
    """Return the balanced contents of ``key(...)`` within ``text``."""
    idx = text.find(key + "(")
    if idx < 0:
        return None
    start = idx + len(key) + 1
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[start : i - 1]


# --- read / write a single feature -----------------------------------------
def get_vcp(bus: int, code: int) -> VcpReading:
    out = _run(["--bus", str(bus), "getvcp", f"{code:02X}", "--terse"]).strip()
    # Formats:
    #   VCP 10 C 75 100                 (continuous: current, max)
    #   VCP 14 SNC x05                  (simple non-continuous: single value)
    #   VCP E2 CNC x00 xff x00 x00      (complex: mh ml sh sl -> we use sl)
    parts = out.split()
    if len(parts) < 3 or parts[0] != "VCP":
        raise DDCError(f"unexpected getvcp output: {out!r}")

    kind_tok = parts[2]
    if kind_tok == "C":
        return VcpReading(
            code=code,
            kind="continuous",
            value=int(parts[3]),
            maximum=int(parts[4]),
        )

    hexvals = [int(p.lstrip("xX"), 16) for p in parts[3:] if p.lower().startswith("x")]
    if kind_tok in ("SNC", "NC"):
        return VcpReading(code=code, kind="simple", value=hexvals[0] if hexvals else 0,
                          raw_bytes=hexvals)
    # CNC / anything else: the low byte (sl, last) carries the selection.
    return VcpReading(code=code, kind="complex", value=hexvals[-1] if hexvals else 0,
                      raw_bytes=hexvals)


def set_vcp(bus: int, code: int, value: int) -> None:
    # --noverify: we read the value back ourselves (see gui._set_and_verify) so
    # we control the pass/fail UX. ddcutil's own verify is too strict for panels
    # that quantise continuous values or reflect complex/manufacturer codes.
    _run(["--bus", str(bus), "setvcp", "--noverify", f"{code:02X}", str(value)])


def set_vcp_word(bus: int, code: int, word: int) -> None:
    """Write a 16-bit value (high byte = sub-code, low byte = value) to a VCP
    code. Used for Dell two-level features like 0xEA USB-C Prioritization
    (e.g. 0xF801 = sub-code 0xF8, value 0x01). ddcutil splits the number into
    SH:SL. --noverify because two-level codes don't read back meaningfully."""
    _run(["--bus", str(bus), "setvcp", "--noverify", f"{code:02X}", f"0x{word:04X}"])


_readonly_cache: dict[int, bool] = {}


def is_read_only(code: int) -> bool:
    """True if the MCCS spec marks this feature read-only (e.g. 0xAA orientation).

    Uses static ``ddcutil vcpinfo`` metadata (no monitor I/O). Manufacturer codes
    with no metadata are treated as *not* read-only — their writability can only
    be judged by attempting a write and reading it back.
    """
    if code in _readonly_cache:
        return _readonly_cache[code]
    try:
        out = _run(["vcpinfo", f"{code:02X}"], timeout=10)
    except DDCError:
        out = ""
    attr_lines = [ln for ln in out.splitlines() if "Attributes" in ln]
    has_write = any("Write" in ln for ln in attr_lines)
    has_ro = any("Read Only" in ln for ln in attr_lines)
    result = bool(attr_lines) and has_ro and not has_write
    _readonly_cache[code] = result
    return result
