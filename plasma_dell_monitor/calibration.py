"""Per-monitor calibration of continuous-feature ranges.

DDC/CI only lets us *read* a continuous feature's current value and maximum; the
usable **minimum** and **step** are not queryable — some Dell panels clamp the
DDC range (e.g. contrast >= 25, gain >= 30) or quantise it (sharpness in 10s)
even though the OSD offers the full 0-100. The only way to learn those limits is
to write probe values and read back what the panel accepts.

Calibration results are cached on disk, keyed by the monitor's serial, so the
(screen-flashing) probe only runs when the user asks for it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

from .ddcutil_backend import get_vcp, set_vcp

_PROBE_SETTLE = 0.1  # seconds between a write and reading it back


@dataclass
class Range:
    minimum: int
    maximum: int
    step: int


def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "plasma-dell-monitor-support", "calibration.json")


def _monitor_key(serial: str, model: str) -> str:
    return serial or f"model:{model}"


def load(serial: str, model: str) -> dict[int, Range]:
    """Return cached ranges ``{code: Range}`` for a monitor, or empty."""
    path = _config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    entry = data.get(_monitor_key(serial, model), {})
    out: dict[int, Range] = {}
    for code_str, r in entry.items():
        try:
            out[int(code_str)] = Range(r["minimum"], r["maximum"], r["step"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def save(serial: str, model: str, ranges: dict[int, Range]) -> None:
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        data = {}
    data[_monitor_key(serial, model)] = {
        str(code): asdict(rng) for code, rng in ranges.items()
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def probe_range(bus: int, code: int, reported_max: int) -> Range:
    """Discover (min, max, step) for one continuous feature, then restore it.

    Writes several low probe values and inspects the distinct read-backs:
      * minimum = what the panel reports after we ask for 0
      * step    = smallest gap between distinct accepted values near the floor
    The feature is returned to its original value before we return.
    """
    original = get_vcp(bus, code).value

    def _set_read(v: int) -> int:
        set_vcp(bus, code, v)
        time.sleep(_PROBE_SETTLE)
        return get_vcp(bus, code).value

    try:
        minimum = _set_read(0)
        # Spread of low targets to expose quantisation without a full sweep.
        targets = sorted({minimum + d for d in (1, 2, 4, 7, 14)})
        reads = sorted({minimum, *(_set_read(t) for t in targets)})
        gaps = [b - a for a, b in zip(reads, reads[1:]) if b > a]
        step = min(gaps) if gaps else 1
    finally:
        set_vcp(bus, code, original)  # always put it back

    return Range(minimum=minimum, maximum=reported_max, step=max(1, step))
