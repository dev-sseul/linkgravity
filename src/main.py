"""Top-level entrypoint. Starts whichever platforms are enabled
(main_discord.run_discord / main_telegram.run_telegram) concurrently in
one process, sharing a single webhook server and exception handler."""

import asyncio

from api import server
from config import DISCORD_TOKEN, SESSION_SCOPES, bot_settings, logger, session_manager


async def main():
    removed = session_manager.cleanup_stale_sessions()
    if removed:
        logger.info(f"Cleaned up {removed} stale session(s) from sessions.json.")

    discord_enabled = bot_settings.get("discord_enabled", bool(DISCORD_TOKEN))
    telegram_enabled = bot_settings.get("telegram_enabled", False)

    if discord_enabled and not SESSION_SCOPES:
        logger.warning("Discord enabled but no server/channel configured (session_scopes) - run `lgy setup`")
    if discord_enabled and not DISCORD_TOKEN:
        logger.critical("Discord enabled but missing DISCORD_TOKEN - run `lgy setup`")
        discord_enabled = False

    if not discord_enabled and not telegram_enabled:
        logger.critical("No messenger platform is enabled - run `lgy setup`")
        return

    def _handle_task_exception(loop, context):
        exc = context.get("exception")
        if exc:
            logger.opt(exception=exc).error(f"Unhandled asyncio task exception: {context.get('message', '')}")
        else:
            logger.error(f"Unhandled asyncio context: {context}")

    asyncio.get_event_loop().set_exception_handler(_handle_task_exception)

    discord_bot = None
    if discord_enabled:
        from main_discord import bot as discord_bot

    # One shared webhook server for every enabled platform - agy always calls this same fixed port.
    asyncio.create_task(server.setup_webhook_server(discord_bot))

    stop_event = asyncio.Event()

    def _handle_sigterm():
        logger.info("Received SIGTERM (lgy stop/restart) - shutting down...")
        stop_event.set()

    try:
        import signal

        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _handle_sigterm)
    except NotImplementedError:
        pass  # add_signal_handler isn't supported on this platform (e.g. Windows)

    tasks = []
    if discord_enabled:
        from main_discord import run_discord

        tasks.append(asyncio.create_task(run_discord(stop_event)))
    if telegram_enabled:
        from main_telegram import run_telegram

        tasks.append(asyncio.create_task(run_telegram(stop_event)))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
