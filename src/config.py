import os
from pathlib import Path

from core.atomic_io import atomic_write_json, safe_load_json

WORKSPACE_DIR = Path.home() / ".gemini" / "linkgravity"
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR = WORKSPACE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Per-user wake-word recordings (see EnrollmentManager); defined early so load_bot_settings' migration below can read it.
WAKE_REF_DIR = WORKSPACE_DIR / "wake_refs"
WAKE_REF_DIR.mkdir(parents=True, exist_ok=True)

LGY_CONFIG_FILE = WORKSPACE_DIR / "lgy.json"

DEFAULT_LGY_CONFIG = {
    "discord_token": "",
    "telegram_token": "",
    "telegram_allowed_user_ids": "",
    "session_scopes": [],
    "allowed_user_ids": "",
    # user_id (str) -> registered word, one per person (see EnrollmentManager._commit_enrollment).
    "wake_words": {},
    "active_timer": 60,
    "voice_threshold": 3000,
    "tts_voice": "ko-KR-SunHiNeural",
    "tts_enabled": True,
    # Sticky default for /new sessions, set whenever /model succeeds.
    "default_model": "",
}


class _PrintLogger:
    """logger.py hasn't been initialized yet at this point in config.py's
    own load order, so this is a minimal stand-in just for the (rare)
    lgy.json-corrupted case."""

    def error(self, msg):
        print(f"[config] {msg}")


def _migrate_legacy_wake_words():
    """Pre-1.3, wake_words was one global string shared by everyone and
    overwritten by each /sound call. The .rpw files were always saved per
    user_id though, so rebuild the real per-user mapping from those."""
    migrated = {}
    for user_dir in WAKE_REF_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        rpw = next(user_dir.glob("*.rpw"), None)
        if rpw:
            migrated[user_dir.name] = rpw.stem.replace("_", " ")
    return migrated


def load_bot_settings():
    data = safe_load_json(LGY_CONFIG_FILE, DEFAULT_LGY_CONFIG.copy(), logger=_PrintLogger())
    for k, v in DEFAULT_LGY_CONFIG.items():
        data.setdefault(k, v)
    if isinstance(data.get("wake_words"), str):
        data["wake_words"] = _migrate_legacy_wake_words()
    return data


def save_bot_settings(data):
    atomic_write_json(LGY_CONFIG_FILE, data)


bot_settings = load_bot_settings()

from core.logger import init_logger
from core.session_manager import SessionManager

logger = init_logger(WORKSPACE_DIR)


DISCORD_TOKEN = bot_settings.get("discord_token", "")
TELEGRAM_TOKEN = bot_settings.get("telegram_token", "")


def _parse_session_scopes(raw_scopes) -> dict:
    """
    Each entry: {"guild_id": "...", "channel_ids": ["...", ...]}.
    An empty/missing channel_ids means "the whole server is allowed" -
    otherwise only the listed channels within that server are allowed.
    Returns {guild_id: frozenset_of_channel_ids_or_None}.
    """
    scopes = {}
    for entry in raw_scopes or []:
        try:
            guild_id = int(entry["guild_id"])
        except (KeyError, TypeError, ValueError):
            continue
        channel_ids = entry.get("channel_ids") or []
        channel_ids = {int(c) for c in channel_ids if str(c).strip()}
        scopes[guild_id] = frozenset(channel_ids) if channel_ids else None
    return scopes


SESSION_SCOPES = _parse_session_scopes(bot_settings.get("session_scopes"))
ALLOWED_IDS = set(int(x) for x in bot_settings.get("allowed_user_ids", "").split(",") if x.strip())
TELEGRAM_ALLOWED_IDS = set(int(x) for x in bot_settings.get("telegram_allowed_user_ids", "").split(",") if x.strip())
TTS_VOICE = bot_settings.get("tts_voice", "ko-KR-SunHiNeural")


def is_allowed_session_channel(channel) -> bool:
    """True if a new agy session may be started from this channel (via
    /new). A channel is allowed if its server is in SESSION_SCOPES AND
    either that server has no channel restriction (whole-server access)
    or this specific channel is in its allowed list."""
    guild = getattr(channel, "guild", None)
    if not guild or guild.id not in SESSION_SCOPES:
        return False
    allowed_channels = SESSION_SCOPES[guild.id]
    return allowed_channels is None or channel.id in allowed_channels


TMP_FILE_DIR = WORKSPACE_DIR / "tmp-files"
TMP_VOICE_DIR = WORKSPACE_DIR / "tmp-voice"

TMP_FILE_DIR.mkdir(parents=True, exist_ok=True)
TMP_VOICE_DIR.mkdir(parents=True, exist_ok=True)

MAX_EMBED_LEN = 1900
STREAM_RATE_LIMIT_SEC = 0.5
APPROVAL_TIMEOUT_SEC = 1800
PERSISTENT_FILE = DATA_DIR / "persistent_tools.json"
SESSION_FILE = DATA_DIR / "sessions.json"

EMBED_COLOR = 0x5865F2

MODEL_CHOICES = {
    "flash": "Gemini 3.5 Flash",
    "flash_lite": "Gemini 3.5 Flash Lite",
    "pro": "Gemini 3.1 Pro",
}

AGY_BIN = os.getenv("AGY_BIN_PATH", str(Path.home() / ".local/bin/agy"))

session_manager = SessionManager(DATA_DIR)


def allowed(user_id: int, platform: str = "discord") -> bool:
    ids = TELEGRAM_ALLOWED_IDS if platform == "telegram" else ALLOWED_IDS
    return not ids or user_id in ids
