import json
import os
from pathlib import Path


def atomic_write_json(path: Path, data) -> None:
    """Writes JSON atomically: writes to a temp file first, then renames it
    into place. os.replace() is atomic on both POSIX and Windows, so a
    crash mid-write leaves the ORIGINAL file untouched instead of a
    half-written, corrupted one."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def safe_load_json(path: Path, default, logger=None):
    """Loads JSON, returning `default` (and backing up the file) if it's
    missing or fails to parse - and actually logging that, instead of
    silently pretending nothing was ever saved."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        if logger:
            logger.error(f"Failed to parse {path}: {e} - backing up as .corrupted and starting fresh")
        try:
            path.replace(path.with_suffix(path.suffix + ".corrupted"))
        except OSError:
            pass
        return default
