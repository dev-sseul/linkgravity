from pathlib import Path

from aiohttp import web

from approval.protected_paths import protected_reason
from config import logger, session_manager
from messengers.registry import get_adapter_for_platform

SIZE_LIMITS = {"discord": 10 * 1024 * 1024, "telegram": 50 * 1024 * 1024, "slack": 50 * 1024 * 1024}


def _resolve_target(thread_id: str) -> tuple[str, dict] | None:
    if thread_id and session_manager.get_session(thread_id):
        return thread_id, session_manager.get_session(thread_id)
    # agy may not pass our env through to the MCP child; with a single live session that is unambiguous.
    sessions = session_manager.get_all_sessions()
    if len(sessions) == 1:
        only = next(iter(sessions.items()))
        return only
    return None


async def handle_attach(request):
    try:
        data = await request.json()
        raw_path = (data.get("path") or "").strip()
        caption = (data.get("caption") or "").strip()
        thread_id = str(data.get("thread_id") or "")

        if not raw_path:
            return web.json_response({"ok": False, "error": "No path given."})

        blocked = protected_reason({"path": raw_path})
        if blocked:
            logger.warning(f"[ATTACH] Blocked protected path: {raw_path!r}")
            return web.json_response({"ok": False, "error": blocked})

        file_path = Path(raw_path).expanduser()
        if not file_path.is_file():
            return web.json_response({"ok": False, "error": f"No such file: {raw_path}"})

        target = _resolve_target(thread_id)
        if not target:
            return web.json_response({"ok": False, "error": "Could not tell which chat to send this to."})
        thread_id, session = target

        platform = session.get("platform", "discord")
        limit = SIZE_LIMITS.get(platform, 10 * 1024 * 1024)
        size = file_path.stat().st_size
        if size > limit:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"File is {size // 1024 // 1024}MB, over the {platform} limit of {limit // 1024 // 1024}MB.",
                }
            )

        adapter = get_adapter_for_platform(platform)
        conversation_ref = adapter.resolve_conversation(thread_id)
        if conversation_ref is None:
            return web.json_response({"ok": False, "error": "That chat is no longer reachable."})

        if caption:
            await adapter.send_message(conversation_ref, caption)
        await adapter.send_files(conversation_ref, [str(file_path)])
        logger.info(f"[ATTACH] Sent {file_path.name} to {platform} thread {thread_id}")
        return web.json_response({"ok": True, "message": f"Attached {file_path.name} in the chat."})
    except Exception as e:
        logger.exception(f"Error in handle_attach: {e}")
        return web.json_response({"ok": False, "error": str(e)})
