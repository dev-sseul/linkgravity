import hashlib
import logging
import os
import tempfile
import urllib.request
from pathlib import Path

# Stdlib only, so the setup wizard can call this before the bot's own config is loaded.
FONT_DIR = Path.home() / ".gemini" / "linkgravity" / "fonts"
FONT_PATH = FONT_DIR / "Pretendard-Regular.otf"
FONT_URL = "https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/public/static/Pretendard-Regular.otf"
# Taken from the official GitHub release file.
FONT_SHA256 = "3ffbacde6ab8411f1d2db54bb9b1f0b3ee2a738932033722cf0388c06aed1c93"

logger = logging.getLogger(__name__)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_font(timeout: float = 20) -> Path | None:
    if FONT_PATH.is_file() and _sha256(FONT_PATH) == FONT_SHA256:
        return FONT_PATH
    try:
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(FONT_URL, timeout=timeout) as resp:
            data = resp.read()
        if hashlib.sha256(data).hexdigest() != FONT_SHA256:
            logger.warning("Downloaded table font failed its checksum - discarding it")
            return None
        fd, tmp = tempfile.mkstemp(dir=FONT_DIR, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, FONT_PATH)
        return FONT_PATH
    except OSError as e:
        logger.warning(f"Could not download the table font: {e}")
        return None


if __name__ == "__main__":
    print("ok" if ensure_font() else "failed")
