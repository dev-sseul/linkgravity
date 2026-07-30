import logging
import os
from pathlib import Path
from sys import stdout

from loguru import logger


def init_logger(workspace_dir: Path):
    logging.getLogger("discord").setLevel(logging.WARNING)
    # httpx is what python-telegram-bot uses under the hood for every getUpdates
    # long-poll request - left unset, it logs each one at INFO, which floods
    # `lgy logs` with a line every poll cycle. httpcore is httpx's own transport
    # layer and is just as noisy at DEBUG, so it's included pre-emptively too.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    LOG_DIR = workspace_dir / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()
    log_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> <level>{level: <5}</level> <cyan>{name}</cyan>: {message}"
    file_format = "{time:YYYY-MM-DD HH:mm:ss} {level: <5} {name}: {message}"
    # Defaults to INFO - set LOG_LEVEL=DEBUG then `lgy restart` for
    # verbose detail (e.g. agy_runner.py's raw agy stdout capture).
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.add(stdout, level=level, format=log_format, colorize=True)
    logger.add(
        LOG_DIR / "bot.log",
        format=file_format,
        level=level,
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
    )
    return logger
