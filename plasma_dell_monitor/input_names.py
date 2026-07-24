"""Per-monitor custom input-source labels (app-side only).

Lets the user relabel input-source values (e.g. "HDMI-1" -> "Work Laptop") for
display in this app's UI. This is cosmetic and local — it does NOT rename the
input on the monitor itself (that would need a Dell manufacturer opcode we don't
have yet; see DDC_ROADMAP.md). Stored by monitor serial, next to the calibration
config.
"""

from __future__ import annotations

import json
import os


def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "plasma-dell-monitor-support", "input_names.json")


def _key(serial: str, model: str) -> str:
    return serial or f"model:{model}"


def load(serial: str, model: str) -> dict[int, str]:
    """Return ``{input_value: custom_name}`` for a monitor, or empty."""
    try:
        with open(_config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    entry = data.get(_key(serial, model), {})
    out: dict[int, str] = {}
    for value_str, name in entry.items():
        try:
            if str(name).strip():
                out[int(value_str)] = str(name)
        except (ValueError, TypeError):
            continue
    return out


def save(serial: str, model: str, names: dict[int, str]) -> None:
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        data = {}
    cleaned = {str(v): n for v, n in names.items() if str(n).strip()}
    if cleaned:
        data[_key(serial, model)] = cleaned
    else:
        data.pop(_key(serial, model), None)  # nothing custom -> drop the entry
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
