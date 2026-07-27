import asyncio
import atexit
import os
import subprocess
import sys
from functools import partial

import discord
from discord.ext import commands

from api import server
from config import (
    DISCORD_TOKEN,
    SESSION_SCOPES,
    logger,
    session_manager,
)
from handlers.message_router import handle_message
from services.response import send_agy_response
from services.streaming import stream_thinking_latest
from utils.utils import (
    agy_new_conversation,
    agy_send_message,
    stt,
    tts,
)

voice_process = None
_voice_shutting_down = False
_voice_restart_timestamps = []  # epoch seconds of recent auto-restarts, for the backoff/giveup check below

VOICE_SERVICE_HEALTH_URL = "http://localhost:18081/health"
VOICE_SERVICE_READY_TIMEOUT_SEC = 20


async def _wait_for_voice_service_ready():
    import aiohttp

    deadline = asyncio.get_event_loop().time() + VOICE_SERVICE_READY_TIMEOUT_SEC
    async with aiohttp.ClientSession() as session:
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with session.get(VOICE_SERVICE_HEALTH_URL, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("ready"):
                            logger.info("🎤 Voice service is online and ready.")
                            return
            except Exception:
                pass
            await asyncio.sleep(0.5)
    logger.warning(
        f"Voice service did not report ready within {VOICE_SERVICE_READY_TIMEOUT_SEC}s - "
        "/join may fail until it finishes starting."
    )


STATUS_TEXT_BY_KEYWORDS = [
    (("run_command",), "🖥️ Running command..."),
    (("file", "list"), "🔍 Analyzing files..."),
    (("search", "web"), "🌐 Searching web..."),
    (("replace", "write"), "✍️ Writing code..."),
    (("ask_question",), "❓ Waiting for input..."),
]


def _status_text_for_tool(tool_name: str) -> str:
    for keywords, text in STATUS_TEXT_BY_KEYWORDS:
        if any(keyword in tool_name for keyword in keywords):
            return text
    return f"⚙️ Running {tool_name}..."


def _voice_status_text() -> str | None:
    """None if no guild is connected to voice right now. Otherwise reflects
    whether any connected guild is in its post-wake-word "awake" window -
    filling the gap between Idle and an active text session, since being
    connected to voice and waiting for a wake word isn't really "Idle"."""
    voice_cog = bot.get_cog("VoiceCog")
    if not voice_cog or not voice_cog._voice_state:
        return None
    if any(voice_cog.stt_session.is_active(guild_id) for guild_id in voice_cog._voice_state):
        return "👂 Awake"
    return "💤 Asleep"


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def status_updater_task():
    await bot.wait_until_ready()
    logger.debug("status_updater_task started")
    try:
        last_status = ""
        while True:
            await asyncio.sleep(2)

            full_status = ""
            if not session_manager.has_active_queues():
                full_status = _voice_status_text() or "🟢 Idle"
            else:
                first_t_id = session_manager.get_active_queue_keys()[0]
                sess = session_manager.get_session(first_t_id) or {}
                pending_tool = sess.get("pending_approval_tool")
                tool_name = sess.get("current_tool")
                if pending_tool:
                    full_status = "⏳ Waiting for approval..."
                elif tool_name:
                    full_status = _status_text_for_tool(tool_name)
                else:
                    full_status = "🧠 Thinking..."

            if full_status != last_status:
                logger.debug(f"Status updating to: {full_status}")
                try:
                    await bot.change_presence(
                        status=discord.Status.online,
                        activity=discord.Activity(type=discord.ActivityType.playing, name=full_status),
                    )
                    last_status = full_status
                except discord.HTTPException as e:
                    logger.warning(f"Presence update rate-limited or failed: {e}")
                except Exception as e:
                    logger.warning(f"Presence update error: {e}")
    except asyncio.CancelledError:
        logger.debug("Status updater task was cancelled.")
    except Exception as e:
        logger.exception(f"FATAL ERROR IN STATUS UPDATER: {e}")


def _spawn_voice_process(voice_dir: str) -> subprocess.Popen:
    return subprocess.Popen(["node", "index.js"], cwd=voice_dir)


async def _supervise_voice_process(voice_dir: str):
    """voice_process (voice-service/index.js) can die on its own - most
    notably from the known upstream @discordjs/voice DAVE decrypt bug
    (discordjs/discord.js#11419), which can throw synchronously out of
    an event handler with nothing upstream to catch it. Without this,
    that single crash would leave every voice feature (wake word, STT,
    TTS) dead until someone manually restarts the whole bot.

    Polls every 1s so a crash gets noticed and a restart kicked off
    almost immediately - the "5 crashes in 5 minutes" check below is
    NOT a claim that 5 minutes of intermittent breakage is acceptable;
    it only exists to stop a genuine crash-loop (missing node_modules,
    a port conflict, etc.) from burning CPU forever. Anyone mid-
    recording when Node dies gets told immediately via
    VoiceCog.handle_voice_service_down(), rather than finding out
    whenever a stale request to Node eventually times out.
    """
    global voice_process
    while True:
        await asyncio.sleep(1)
        if _voice_shutting_down:
            return
        if voice_process is None or voice_process.poll() is None:
            continue  # still running (or never started) - nothing to do

        exit_code = voice_process.poll()
        logger.warning(f"🎤 Voice service exited unexpectedly (code {exit_code}). Attempting to restart it...")

        # Notify anyone mid-recording before attempting the restart, not after.
        voice_cog = bot.get_cog("VoiceCog")
        if voice_cog:
            try:
                await voice_cog.handle_voice_service_down()
            except Exception as e:
                logger.error(f"Error notifying users of voice service outage: {e}")

        now = asyncio.get_event_loop().time()
        _voice_restart_timestamps.append(now)
        while _voice_restart_timestamps and now - _voice_restart_timestamps[0] > 300:
            _voice_restart_timestamps.pop(0)

        if len(_voice_restart_timestamps) > 5:
            logger.error(
                "🎤 Voice service has crashed 5+ times in the last 5 minutes - giving up on auto-restart "
                "to avoid a crash loop. Check `lgy logs` for the underlying error, fix it, then run "
                "`lgy restart`."
            )
            return

        try:
            voice_process = _spawn_voice_process(voice_dir)
            asyncio.create_task(_wait_for_voice_service_ready())
        except Exception as e:
            logger.error(f"Failed to restart voice service: {e}")


@bot.event
async def on_ready():
    logger.info(f"✅ Bot is fully online and ready! Logged in as {bot.user}")


def _terminate_voice_process():
    global _voice_shutting_down
    _voice_shutting_down = True
    if voice_process and voice_process.poll() is None:
        logger.debug("Terminating child Node.js voice process...")
        voice_process.terminate()  # Node now catches this and disconnects any active voice channel cleanly
        try:
            voice_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            voice_process.kill()


@bot.event
async def setup_hook():
    await bot.tree.sync()
    logger.info("Slash commands synced.")

    asyncio.create_task(server.setup_webhook_server(bot))

    global voice_process
    try:
        voice_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "voice-service")
        logger.debug(f"Spawning Node.js process at: {voice_dir}")
        if os.path.exists(os.path.join(voice_dir, "index.js")):
            if not os.path.isdir(os.path.join(voice_dir, "node_modules")):
                logger.error(
                    "voice-service/node_modules is missing - its dependencies were never installed "
                    "(or got wiped, e.g. by replacing project files without re-running `npm install`). "
                    "Voice features will not work until you run `npm install` in the project root "
                    "and restart the bot."
                )
            voice_process = _spawn_voice_process(voice_dir)
            logger.info("🎤 Voice service starting, waiting for it to come online...")
            asyncio.create_task(_wait_for_voice_service_ready())
            asyncio.create_task(_supervise_voice_process(voice_dir))

            atexit.register(_terminate_voice_process)
        else:
            logger.warning("Voice service (index.js) not found. Skipping auto-start.")
    except Exception as e:
        logger.error(f"Failed to auto-start voice service: {e}")

    def _handle_task_exception(loop, context):
        exc = context.get("exception")
        if exc:
            logger.opt(exception=exc).error(f"Unhandled asyncio task exception: {context.get('message', '')}")
        else:
            logger.error(f"Unhandled asyncio context: {context}")

    asyncio.get_event_loop().set_exception_handler(_handle_task_exception)

    logger.debug("Launching status_updater_task...")
    asyncio.create_task(status_updater_task())
    logger.debug("status_updater_task dispatched")


@bot.event
async def on_error(event_method: str, *args, **kwargs):
    logger.exception(f"Unhandled exception in Discord event: {event_method}")
    exc_type, exc_value, _ = sys.exc_info()
    if exc_value is None:
        return

    error_msg = "⚠️ **An internal bot error has occurred.** Please contact the developer or check the server logs."

    if event_method == "on_message":
        if args and isinstance(args[0], discord.Message):
            message = args[0]
            try:
                await message.reply(error_msg)
            except Exception:
                pass


@bot.event
async def on_application_command_error(interaction, error):
    logger.exception(f"Slash command error: {error}")
    error_msg = "⚠️ **An error occurred while processing the command.** Please try again later or check the logs."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(error_msg, ephemeral=True)
        else:
            await interaction.response.send_message(error_msg, ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_message(message: discord.Message):
    await handle_message(bot, message)


async def main():
    if not DISCORD_TOKEN or not SESSION_SCOPES:
        logger.critical("Missing DISCORD_TOKEN, or no server/channel configured (session_scopes) - run `lgy setup`")
        return

    discord.utils.setup_logging()

    def _handle_sigterm():
        logger.info("Received SIGTERM (lgy stop/restart) - disconnecting voice before exit...")
        _terminate_voice_process()
        asyncio.create_task(bot.close())

    try:
        import signal

        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _handle_sigterm)
    except NotImplementedError:
        pass  # add_signal_handler isn't supported on this platform (e.g. Windows)

    from messengers.discord_adapter import DiscordAdapter
    from messengers.registry import set_adapter

    set_adapter(DiscordAdapter(bot))

    async with bot:
        from cogs.voice_cog import VoiceCog
        from config import bot_settings, save_bot_settings

        await bot.add_cog(
            VoiceCog(
                bot=bot,
                stt=stt,
                tts=tts,
                send_agy_response=send_agy_response,
                agy_send=agy_send_message,
                stream_thinking_latest=partial(stream_thinking_latest, bot),
                agy_start_session=agy_new_conversation,
                session_manager=session_manager,
                bot_settings=bot_settings,
                save_bot_settings=save_bot_settings,
                logger=logger,
            )
        )

        from cogs.general_cog import GeneralCog

        await bot.add_cog(GeneralCog(bot=bot))
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
