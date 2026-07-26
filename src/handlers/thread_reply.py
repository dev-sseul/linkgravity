import asyncio
import time
from datetime import datetime

import discord

from config import session_manager
from messengers.registry import get_adapter
from services.response import render_thought_process, send_agy_response
from services.streaming import stream_thinking_latest
from utils.utils import (
    agy_new_conversation,
    agy_send_message,
    build_content_with_images,
    cleanup_images,
    generate_thread_title,
    handle_image_attachments,
    stt,
)


async def handle_approval_reply(
    message: discord.Message, thread: discord.Thread, session: dict, content: str, pa
) -> bool:
    adapter = get_adapter()
    if content.lower() in ("yes", "y", "allow", "승인"):
        pa.set_result("allow")
        await adapter.send_message(thread, f'✅ *Answer Received (Write in): "{content}"*')
        return True
    elif content.lower() in ("c", "cancel"):
        pa.set_result("allow")
        await adapter.send_message(thread, "✅ *Text Approval Received*")
        return True
    elif content.lower() in ("no", "n", "reject", "거절"):
        pa.set_result("reject")
        await adapter.send_message(thread, "❌ *Text Rejection Received*")
        return True
    elif content.lower() in ("clear", "reset"):
        await adapter.send_message(thread, "🧹 Conversation context cleared.")
        old_sess = session_manager.remove_session(str(thread.id))
        if old_sess:
            session_manager.set_session(str(thread.id), old_sess)
        return True
    return False


async def handle_pending_session(
    bot, thread: discord.Thread, session: dict, agy_content: str, content: str, image_paths: list
):
    adapter = get_adapter()
    try:
        async with adapter.typing(thread):
            ctx = {"status_msg": None}
            start_time = time.time()
            queue = asyncio.Queue()
            session_manager.register_queue(str(thread.id), queue)
            stream_task = asyncio.create_task(stream_thinking_latest(bot, thread, context_dict=ctx, queue=queue))

            cwd = session.get("cwd")
            model = session.get("model")
            result_text, new_conv_id = await agy_new_conversation(
                agy_content, model=model, stream_queue=queue, thread_id=str(thread.id), cwd=cwd
            )

            await queue.put(("__END__", True))
            await stream_task

            response_text = result_text
            new_title = await generate_thread_title(content, response_text)
            await adapter.rename_conversation(thread, new_title)

            response_text = await render_thought_process(new_conv_id, ctx, response_text, thread)

            session["conversation_id"] = new_conv_id
            session["created_at"] = datetime.now().isoformat()
            session["status"] = "active"
            session_manager.save_sessions()

            await send_agy_response(thread, response_text, session, ctx, start_time, new_conv_id)
    finally:
        cleanup_images(image_paths)


async def handle_existing_session(
    bot, thread: discord.Thread, session: dict, conv_id: str, agy_content: str, image_paths: list
):
    adapter = get_adapter()
    try:
        async with adapter.typing(thread):
            ctx = {"status_msg": None}
            start_time = time.time()
            queue = asyncio.Queue()
            session_manager.register_queue(str(thread.id), queue)
            stream_task = asyncio.create_task(stream_thinking_latest(bot, thread, context_dict=ctx, queue=queue))
            result_text = await agy_send_message(
                conv_id,
                agy_content,
                model=session.get("model"),
                stream_queue=queue,
                thread_id=str(thread.id),
                cwd=session.get("cwd"),
            )
            await queue.put(("__END__", True))
            await stream_task

            response_text = result_text
            response_text = await render_thought_process(conv_id, ctx, response_text, thread)
            await send_agy_response(thread, response_text, session, ctx, start_time, conv_id)
    finally:
        cleanup_images(image_paths)


async def handle_thread_reply(bot, message: discord.Message):
    thread = message.channel
    session = session_manager.get_session(str(thread.id))
    if not session:
        return

    adapter = get_adapter()
    content = message.content.strip()
    if content.startswith("/new"):
        await adapter.send_message(
            thread,
            "To start a new session, please use the `/new` slash command (not typed as text) - it works from inside a thread too and will open the new one in the right place.",
        )
        return

    for att in message.attachments:
        ct = att.content_type or ""
        if "audio" in ct or att.filename.endswith((".ogg", ".mp3", ".m4a", ".wav")):
            await message.add_reaction("🎤")
            audio_bytes = await att.read()
            text = await stt(audio_bytes)
            if text:
                content = text
                await adapter.send_message(thread, f'🎤 *Speech Recognized: "{text}"*')
            break

    image_paths = await handle_image_attachments(message)
    if image_paths:
        await message.add_reaction("📎")
        await adapter.send_message(thread, f"📎 *{len(image_paths)} file(s) attached*")

    if not content and not image_paths:
        return
    if not content:
        content = "Analyze this attachment"

    agy_content = build_content_with_images(content, image_paths)
    conv_id = session.get("conversation_id")
    pa = session_manager.get_pending_approval_by_conv(conv_id) if conv_id else None

    if conv_id and pa and not pa.done():
        handled = await handle_approval_reply(message, thread, session, content, pa)
        if handled:
            return

    if not conv_id:
        if session.get("status") == "pending":
            await handle_pending_session(bot, thread, session, agy_content, content, image_paths)
            return
        else:
            await adapter.send_message(thread, "⚠️ Session ID not found. Start a new session with `/new`.")
            return

    await handle_existing_session(bot, thread, session, conv_id, agy_content, image_paths)
