"""Small persistent app-wide preferences (not per-monitor), stored as JSON next to
the other config — e.g. whether the user has permanently disabled the import
skip-warning dialogs.
"""

from __future__ import annotations

import json
import os


def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "plasma-dell-monitor-support", "settings.json")


def get(key: str, default=None):
    try:
        with open(_config_path(), encoding="utf-8") as fh:
            return json.load(fh).get(key, default)
    except (FileNotFoundError, ValueError):
        return default


def set(key: str, value) -> None:  # noqa: A001 - mirrors dict.get/set naming here
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        data = {}
    data[key] = value
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
