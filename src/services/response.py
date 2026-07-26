from pathlib import Path
from typing import Any

from config import MAX_EMBED_LEN, MODEL_CHOICES, session_manager
from messengers.registry import get_adapter
from utils.utils import get_current_model


async def send_agy_response(
    thread: Any,
    response_text: str,
    session: dict,
    ctx: dict = None,
    start_time: float = 0,
    conv_id: str = None,
):
    adapter = get_adapter()
    session_manager.save_sessions()

    parts = [response_text[i : i + MAX_EMBED_LEN] for i in range(0, max(len(response_text), 1), MAX_EMBED_LEN)]
    for idx, part in enumerate(parts):
        is_last = idx == len(parts) - 1

        if is_last:
            if not part.strip():
                continue

            session_model = session.get("model")
            model_display = MODEL_CHOICES.get(session_model, session_model) if session_model else get_current_model()
            text_to_send = f"{part}\n-# 🤖 {model_display}"

            status_msg = ctx.get("status_msg") if ctx else None
            if status_msg and await adapter.edit_message(status_msg, text_to_send):
                continue
            await adapter.send_message(thread, text_to_send)
        else:
            await adapter.send_message(thread, part)

    files_to_send = []
    if conv_id and start_time:
        brain_dir = Path.home() / f".gemini/antigravity-cli/brain/{conv_id}"
        if brain_dir.exists():
            for md_file in brain_dir.glob("*.md"):
                if md_file.stat().st_mtime >= start_time:
                    files_to_send.append(str(md_file))
    if files_to_send:
        await adapter.send_files(thread, files_to_send)


async def render_thought_process(conv_id: str, ctx: dict, response_text: str, thread: Any) -> str:
    return ctx.get("final_text", response_text)
