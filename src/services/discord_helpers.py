import re
import string
import uuid
from pathlib import Path

from config import TMP_FILE_DIR, logger
from messengers.base import IncomingAttachment


async def handle_image_attachments(attachments: list[IncomingAttachment]) -> list[str]:
    saved_paths = []
    for att in attachments:
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
        "그래",
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


def split_message(text: str, limit: int) -> list[str]:
    parts = []
    fence = None
    remaining = text
    while remaining:
        # A chunk that ends mid-code-block gets closed here and reopened at the top of the next one,
        # otherwise the client renders the rest of the message as one runaway code block.
        prefix = f"{fence}\n" if fence else ""
        budget = limit - len(prefix) - len("\n```")
        if len(remaining) <= budget:
            body, remaining = remaining, ""
        else:
            window = remaining[:budget]
            cuts = [pos + len(d) for d in ("\n\n", "\n", " ") if (pos := window.rfind(d)) > 0]
            cut = next((c for c in cuts if c > budget // 2), max(cuts, default=0))
            body, remaining = remaining[: cut or budget], remaining[cut or budget :]
        chunk = prefix + body
        fence = None
        for match in re.finditer(r"^```(\S*)", chunk, re.MULTILINE):
            fence = None if fence else f"```{match.group(1)}"
        if fence:
            chunk += "\n```"
        if parts and not re.sub(r"^```\S*$", "", chunk, flags=re.MULTILINE).strip():
            continue
        parts.append(chunk)
    return parts or [""]
