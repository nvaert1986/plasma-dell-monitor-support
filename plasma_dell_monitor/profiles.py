"""Per-monitor saved profiles — up to 10 slots (0-9) of the visual/image settings.

Stored by monitor serial, next to the other config. Each slot is
``{"label": str, "settings": {...}}`` where ``settings`` has the same shape as an
exported settings file (image settings only — brightness/contrast/sharpness/RGB
gain/colour preset). Loading a profile reuses the same apply engine as import.
"""

from __future__ import annotations

import json
import os

NUM_SLOTS = 10


def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "plasma-dell-monitor-support", "profiles.json")


def _key(serial: str, model: str) -> str:
    return serial or f"model:{model}"


def _load_all() -> dict:
    try:
        with open(_config_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def load(serial: str, model: str) -> dict[int, dict]:
    """Return ``{slot: {"label", "settings"}}`` for this monitor's filled slots."""
    entry = _load_all().get(_key(serial, model), {})
    out: dict[int, dict] = {}
    for k, v in entry.items():
        try:
            slot = int(k)
        except (ValueError, TypeError):
            continue
        if 0 <= slot < NUM_SLOTS and isinstance(v, dict):
            out[slot] = {"label": str(v.get("label", "")),
                         "settings": v.get("settings") or {}}
    return out


def get_slot(serial: str, model: str, slot: int) -> dict | None:
    return load(serial, model).get(int(slot))


def save_slot(serial: str, model: str, slot: int, label: str, settings: dict) -> None:
    data = _load_all()
    key = _key(serial, model)
    entry = data.get(key) or {}
    entry[str(int(slot))] = {"label": str(label or ""), "settings": settings or {}}
    data[key] = entry
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
