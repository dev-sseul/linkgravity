import asyncio

from aiohttp import web

from config import logger


async def handle_stt_input(request):
    try:
        data = await request.json()
        bot = request.app["bot"]
        cog = bot.get_cog("VoiceCog")
        if cog:
            asyncio.create_task(cog.handle_stt_input(data))
        return web.json_response({"success": True})
    except Exception as e:
        import traceback

        logger.error(f"STT API error: {e}")
        traceback.print_exc()
        return web.json_response({"error": str(e)}, status=500)


async def handle_tts_finished(request):
    """Called by voice-service/index.js the moment the bot's spoken reply
    actually finishes playing (its audio queue drains). This is what
    should start the "keep listening" countdown - not the moment the
    user's speech was recognized, which is well before the bot has even
    started replying."""
    try:
        data = await request.json()
        guild_id = data.get("guild_id")
        bot = request.app["bot"]
        cog = bot.get_cog("VoiceCog")
        if cog and guild_id:
            cog.mark_tts_finished(str(guild_id))
        return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"tts_finished API error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_stt_partial(request):
    """Called repeatedly by voice-service/index.js while the user is
    still speaking, only during the post-wake active window (bounded,
    default 60s - not always-on). Body is already-recognized {"text":
    ...}, not audio; this just updates a live "listening..." message."""
    try:
        data = await request.json()
        bot = request.app["bot"]
        cog = bot.get_cog("VoiceCog")
        if cog:
            asyncio.create_task(cog.handle_stt_partial(data))
        return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"stt_partial API error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_stt_partial_cancel(request):
    """Called when an utterance that had a live partial message showing
    turned out too short to actually process, or got dropped because the
    active window lapsed mid-utterance - cleans up that placeholder
    instead of leaving "🎤 (listening...)" stuck forever."""
    try:
        data = await request.json()
        guild_id = data.get("guild_id")
        bot = request.app["bot"]
        cog = bot.get_cog("VoiceCog")
        if cog and guild_id:
            asyncio.create_task(cog.cancel_stt_partial(str(guild_id)))
        return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"stt_partial_cancel API error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_enroll_sample(request):
    """Called by voice-service/index.js for each utterance a user makes
    WHILE enrolling a wake word (see its `enrollingUsers` set) - raw
    PCM/WAV bytes, same as /wake_check. Nothing gets matched against
    this; it's just collected as a reference sample. See
    VoiceCog.handle_enroll_sample for where samples actually get saved
    (only once all of them are in)."""
    try:
        user_id = request.query.get("user_id")
        audio_bytes = await request.read()
        cog = request.app["bot"].get_cog("VoiceCog")
        if cog and user_id and audio_bytes:
            asyncio.create_task(cog.handle_enroll_sample(user_id, audio_bytes))
        return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"enroll_sample API error: {e}")
        return web.json_response({"error": str(e)}, status=500)
