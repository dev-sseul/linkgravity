import re
import string
import uuid
from pathlib import Path

import discord

from config import TMP_FILE_DIR, logger


async def handle_image_attachments(message: discord.Message) -> list[str]:
    saved_paths = []
    for att in message.attachments:
        ext = Path(att.filename).suffix.lower()
        filename = f"{uuid.uuid4().hex}{ext or '.tmp'}"
        dest = TMP_FILE_DIR / filename
        try:
            data = await att.read()
            dest.write_bytes(data)
            saved_paths.append(str(dest))
        except Exception as e:
            logger.error(f"Image save failed: {e}")
    return saved_paths


def cleanup_images(image_paths: list[str]):
    for path in image_paths:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to delete temp image {path}: {e}")


def build_content_with_images(content: str, image_paths: list[str]) -> str:
    parts = []
    parts.append(content)

    if not image_paths:
        return "\n".join(parts)

    parts.append("\n\n[Attached file path (use `view_file` tool to analyze)]")
    for path in image_paths:
        parts.append(f"- {path}")
    return "\n".join(parts)


def clean_ansi(text: str) -> str:
    return re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])").sub("", text)


def check_approval_intent(text: str) -> str:
    lower_c = text.strip().lower()
    neg_kr_substr = ["아니", "거절", "취소", "하지마", "안돼", "싫어", "멈춰", "그만", "안해", "노노"]
    for neg in neg_kr_substr:
        if neg in lower_c:
            return "reject"
    pos_kr_substr = ["진행", "승인", "하라고", "해봐", "해라", "실행", "그래", "알았어", "좋아", "수락", "오케이"]
    for pos in pos_kr_substr:
        if pos in lower_c:
            return "allow"

    clean_text = lower_c.translate(str.maketrans("", "", string.punctuation))
    words = clean_text.split()
    exact_reject = ["no", "cancel", "stop", "reject", "deny", "n", "ㄴㄴ", "nope", "abort", "quit", "never"]
    exact_allow = [
        "yes",
        "ok",
        "okay",
        "go",
        "approve",
        "allow",
        "y",
        "응",
        "어",
        "ㅇㅇ",
        "ㅇㅋ",
        "해",
        "콜",
        "네",
        "sure",
        "yeah",
        "yep",
        "yup",
        "proceed",
        "fine",
        "alright",
        "고",
    ]
    for word in words:
        if word in exact_reject:
            return "reject"
    for word in words:
        if word in exact_allow:
            return "allow"
    return None
