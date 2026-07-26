"""The live "listening..." placeholder message and the post-wake "stay awake" window."""

import time

import aiohttp
import discord

NODE_VOICE_API = "http://localhost:18081"
NODE_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5)


class SttSessionTracker:
    def __init__(self, bot, voice_state: dict, bot_settings: dict, logger):
        self.bot = bot
        self._voice_state = voice_state  # shared with VoiceCog
        self.bot_settings = bot_settings
        self.logger = logger
        self._last_active_time = {}
        self._partial_msg = {}

    def is_active(self, guild_id: str) -> bool:
        """Whether the "stay awake" window is still open for this guild."""
        active_duration = self.bot_settings.get("active_timer", 60)
        last_active = self._last_active_time.get(str(guild_id), 0)
        return (time.time() - last_active) < active_duration

    def mark_tts_finished(self, guild_id: str):
        """Called via /tts_finished once the spoken reply finishes playing."""
        self.extend_active_window(str(guild_id))

    def extend_active_window(self, guild_id: str):
        """Starts/renews the "stay awake" window, locally and on Node."""
        import asyncio

        self._last_active_time[guild_id] = time.time()
        active_duration = self.bot_settings.get("active_timer", 60)
        active_until_ms = int((time.time() + active_duration) * 1000)
        asyncio.create_task(self._sync_active_window_to_node(guild_id, active_until_ms))

    def clear_active_window(self, guild_id: str):
        """Ends the window immediately, locally and on Node - called on
        /join so a fresh connection doesn't inherit one left open."""
        import asyncio

        self._last_active_time.pop(str(guild_id), None)
        asyncio.create_task(self._sync_active_window_to_node(guild_id, 0))

    async def _sync_active_window_to_node(self, guild_id: str, active_until_ms: int):
        try:
            async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as session:
                await session.post(
                    f"{NODE_VOICE_API}/set_active",
                    json={"guild_id": guild_id, "active_until": active_until_ms},
                )
        except aiohttp.ClientError as e:
            self.logger.warning(f"Failed to sync active window to Node for guild {guild_id}: {e}")

    async def handle_stt_partial(self, data: dict):
        """Called by voice-service while the user speaks, only during the
        active window. Just updates a live "listening..." placeholder -
        handle_stt_input's final call is what reaches the LLM."""
        try:
            guild_id = str(data.get("guild_id"))
            text = data.get("text")
            thread_id = self._voice_state.get(guild_id)
            if not thread_id:
                return
            thread = self.bot.get_channel(int(thread_id))
            if not thread:
                return

            display_text = text if text else "..."
            content = f"🎤 *(listening...)* {display_text}"

            existing = self._partial_msg.get(guild_id)
            if existing:
                try:
                    await existing.edit(content=content)
                    return
                except discord.NotFound:
                    self._partial_msg.pop(guild_id, None)
                except discord.HTTPException as e:
                    self.logger.warning(f"Failed to edit partial STT message: {e}")
                    return

            try:
                self._partial_msg[guild_id] = await thread.send(content)
            except discord.HTTPException as e:
                self.logger.warning(f"Failed to send partial STT message: {e}")
        except Exception as e:
            self.logger.exception(f"Error in handle_stt_partial: {e}")

    async def cancel_stt_partial(self, guild_id: str):
        """Called when an utterance that had a live partial message
        showing turned out too short, or the active window lapsed
        mid-utterance so the final result got dropped."""
        await self.clear_partial_msg(str(guild_id))

    async def clear_partial_msg(self, guild_id: str):
        msg = self._partial_msg.pop(guild_id, None)
        if not msg:
            return
        try:
            await msg.delete()
        except discord.HTTPException:
            pass

    async def finalize_partial_msg(self, guild_id: str, thread, final_content: str):
        """Turns the live "listening..." placeholder into the final
        recognized-text message, if one exists; otherwise sends it fresh
        (e.g. the utterance was short enough that no partial ever fired)."""
        msg = self._partial_msg.pop(guild_id, None)
        if msg:
            try:
                await msg.edit(content=final_content)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                self.logger.warning(f"Failed to finalize partial STT message: {e}")
        await thread.send(final_content)
