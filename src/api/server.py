from aiohttp import web

from config import logger, session_manager


def is_tool_allowed(tool_name, tool_input):
    if tool_name == "manage_task" and tool_input.get("Action") in ["list", "status", "kill", "send_input"]:
        return True
    if tool_name == "list_dir":
        return True
    if tool_name == "view_file":
        return True
    if tool_name == "read_file":
        return True
    if tool_name == "read_url_content":
        return True
    if tool_name in session_manager.persistent_allowed.get("tools", []):
        return True
    return False


async def setup_webhook_server(bot):
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app["bot"] = bot

    from api.ui_routes import handle_approve_request
    from api.voice_routes import (
        handle_enroll_sample,
        handle_stt_input,
        handle_stt_partial,
        handle_stt_partial_cancel,
        handle_tts_finished,
    )

    app.router.add_post("/approve", handle_approve_request)
    app.router.add_post("/stt_input", handle_stt_input)
    app.router.add_post("/tts_finished", handle_tts_finished)
    app.router.add_post("/stt_partial", handle_stt_partial)
    app.router.add_post("/stt_partial_cancel", handle_stt_partial_cancel)
    app.router.add_post("/enroll_sample", handle_enroll_sample)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18080)
    await site.start()
    logger.info("Webhook / STT Server started on port 18080")
