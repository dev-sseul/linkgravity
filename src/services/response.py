import asyncio
import shutil
from pathlib import Path
from typing import Any

from config import MAX_EMBED_LEN, bot_settings, logger, session_manager
from messengers.base import PhotoLimits
from messengers.registry import get_adapter_for_platform
from services import tables
from services.discord_helpers import split_message
from services.fonts import ensure_font
from utils.utils import get_current_model


async def send_agy_response(
    thread: Any,
    response_text: str,
    session: dict,
    ctx: dict = None,
    start_time: float = 0,
    conv_id: str = None,
):
    adapter = get_adapter_for_platform(session.get("platform", "discord"))
    session_manager.save_sessions()

    # Same precedence as the /model autocomplete in general_cog.
    model_display = session.get("model") or bot_settings.get("default_model") or get_current_model()

    footer = f"-# 🤖 {model_display}"
    status_msg = ctx.get("status_msg") if ctx else None

    async def emit(text: str):
        nonlocal status_msg
        msg, status_msg = status_msg, None
        if msg and await adapter.edit_message(msg, text):
            return
        await adapter.send_message(thread, text)

    segments = tables.split_tables(response_text)
    has_table = any(kind == "table" for kind, _ in segments)
    font = await asyncio.to_thread(ensure_font) if has_table else None
    if not font:
        segments = [("text", response_text)]

    footer_sent = False
    table_no = 0
    for seg_idx, (kind, value) in enumerate(segments):
        if kind == "table":
            table_no += 1
            name = f"table_{table_no}"
            out_dir = tables.new_output_dir()
            try:
                images = await _render_table(value, font, out_dir, adapter.photo_limits, name)
                if images:
                    if status_msg:
                        # The streamed preview holds the raw markdown; replace it so it isn't shown twice.
                        await emit("📊")
                    await adapter.send_images(thread, images)
                    await adapter.send_files(thread, [str(p) for p in tables.write_attachments(value, out_dir, name)])
                    continue
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)
            value = tables.to_markdown(value)
        parts = split_message(value, MAX_EMBED_LEN)
        for idx, part in enumerate(parts):
            if seg_idx == len(segments) - 1 and idx == len(parts) - 1:
                if not part.strip():
                    continue
                part = f"{part}\n{footer}"
                footer_sent = True
            await emit(part)
    if not footer_sent and segments[-1][0] == "table":
        await emit(footer)

    files_to_send = []
    if conv_id and start_time:
        brain_dir = Path.home() / f".gemini/antigravity-cli/brain/{conv_id}"
        if brain_dir.exists():
            for md_file in brain_dir.glob("*.md"):
                if md_file.stat().st_mtime >= start_time:
                    files_to_send.append(str(md_file))
    if files_to_send:
        await adapter.send_files(thread, files_to_send)


async def _render_table(
    rows: list[list[str]], font: Path, out_dir: Path, photo_limits: PhotoLimits | None, name: str
) -> list[str] | None:
    try:
        return [str(p) for p in await asyncio.to_thread(tables.render_table, rows, font, out_dir, photo_limits, name)]
    except Exception as e:
        logger.exception(f"Table render failed, falling back to text: {e}")
        return None


async def render_thought_process(conv_id: str, ctx: dict, response_text: str, thread: Any) -> str:
    return ctx.get("final_text", response_text)
