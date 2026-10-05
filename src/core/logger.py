import logging
import os
from pathlib import Path
from sys import stdout

from loguru import logger


class _InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Without patching, {name} resolves to this file's frame instead of the library that logged.
        patched = logger.patch(lambda r, name=record.name: r.update(name=name))
        patched.opt(exception=record.exc_info).log(level, record.getMessage())


def init_logger(workspace_dir: Path):
    logging.getLogger("discord").setLevel(logging.WARNING)
    # python-telegram-bot's httpx logs every long-poll at INFO, and httpcore is as noisy at DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    LOG_DIR = workspace_dir / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()
    log_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> <level>{level: <5}</level> <cyan>{name}</cyan>: {message}"
    file_format = "{time:YYYY-MM-DD HH:mm:ss} {level: <5} {name}: {message}"
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    # force=True drops handlers libraries install themselves, which would print in their own format.
    logging.basicConfig(handlers=[_InterceptHandler()], level=getattr(logging, level, logging.INFO), force=True)
    # diagnose would print local variables in tracebacks, and SDK client reprs include the bot tokens.
    logger.add(stdout, level=level, format=log_format, colorize=True, diagnose=False)
    logger.add(
        LOG_DIR / "bot.log",
        format=file_format,
        level=level,
        diagnose=False,
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
    )
    return logger
