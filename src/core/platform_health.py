"""Tracks each platform's live connection health, separately from the static "enabled" config flag."""

from datetime import datetime
from pathlib import Path

from core.atomic_io import atomic_write_json, safe_load_json


def _path() -> Path:
    from config import DATA_DIR

    return DATA_DIR / "platform_health.json"


def set_status(platform: str, status: str, detail: str = "") -> None:
    """status: 'connecting' | 'running' | 'error'"""
    path = _path()
    data = safe_load_json(path, {})
    data[platform] = {
        "status": status,
        "detail": detail,
        "at": datetime.now().isoformat(),
    }
    atomic_write_json(path, data)


def get_all() -> dict:
    return safe_load_json(_path(), {})
