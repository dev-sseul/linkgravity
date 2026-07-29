import asyncio
import re
import time
from typing import Any

import discord  # only for voice/TTS text cleanup below; messaging goes through the adapter

from config import MAX_EMBED_LEN, STREAM_RATE_LIMIT_SEC, bot_settings, logger, session_manager
from messengers.registry import get_adapter_for_thread
from utils.utils import clean_ansi


def _clear_current_tool(thread_id: str):
    """Removes the "current_tool"/"pending_approval_tool" markers set by
    api/ui_routes.py while a tool call is being approved/run - without
    this, the bot's presence status stays stuck on the last tool after
    the turn finishes."""
    session = session_manager.get_session(thread_id)
    if not session:
        return
    changed = False
    for key in ("current_tool", "pending_approval_tool"):
        if key in session:
            del session[key]
            changed = True
    if changed:
        session_manager.set_session(thread_id, session)


class StreamUpdater:
    def __init__(self, thread: Any, thread_id: str, context_dict: dict):
        self.MAX_EMBED_LEN = MAX_EMBED_LEN
        self.RATE_LIMIT_SEC = STREAM_RATE_LIMIT_SEC
        self.thread = thread
        self.thread_id = thread_id
        self.context_dict = context_dict
        self.status_msg = None
        self.current_text = ""
        self.last_update_time = time.time()
        if self.context_dict is not None:
            self.context_dict["status_msg"] = None

    async def process_chunk(self, chunk: str):
        self.current_text += chunk

    async def split(self):
        full_text = self.current_text.strip()
        if full_text:
            parts = [full_text[i : i + self.MAX_EMBED_LEN] for i in range(0, len(full_text), self.MAX_EMBED_LEN)]
            for idx, part in enumerate(parts):
                await self._update(part, force_new=(idx > 0))
        self.status_msg = None
        self.current_text = ""
        if self.context_dict is not None:
            self.context_dict["status_msg"] = None

    async def flush(self, force=False):
        now = time.time()
        if force or now - self.last_update_time >= self.RATE_LIMIT_SEC:
            display_text = self.current_text[-self.MAX_EMBED_LEN :].strip()
            if display_text:
                await self._update(display_text, force_new=False)
                if self.context_dict is not None:
                    self.context_dict["final_text"] = self.current_text.strip()
            self.last_update_time = now

    async def _update(self, text: str, force_new: bool):
        adapter = get_adapter_for_thread(self.thread_id)
        try:
            if self.status_msg is not None and not force_new:
                if await adapter.edit_message(self.status_msg, text):
                    return
                # Edit failed (message gone) - fall through to sending anew.
                self.status_msg = None
            self.status_msg = await adapter.send_message(self.thread, text)
            if self.context_dict is not None:
                self.context_dict["status_msg"] = self.status_msg
        except asyncio.CancelledError:
            logger.debug("Stream update was cancelled.")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error in stream update: {e}")


class TTSStreamManager:
    def __init__(self, thread_id: str, guild_id: str, cog, is_voice: bool):
        self.thread_id = str(thread_id)
        self.guild_id = str(guild_id) if guild_id else None
        self.cog = cog
        self.is_voice = is_voice
        self.tts_buffer = ""
        self.settings = bot_settings

    async def process_chunk(self, chunk: str):
        if not self.is_voice:
            return
        self.tts_buffer += chunk
        if re.search(r"([.?!]\s+|\n+)", self.tts_buffer):
            parts = re.split(r"([.?!]\s+|\n+)", self.tts_buffer)
            complete_sentences = ""
            for i in range(0, len(parts) - 1, 2):
                complete_sentences += parts[i] + parts[i + 1]
            self.tts_buffer = parts[-1]
            await self._queue_tts(complete_sentences)

    async def flush_all(self):
        if not self.is_voice:
            return
        text = self.tts_buffer.strip()
        self.tts_buffer = ""
        if text:
            await self._queue_tts(text)

    async def _queue_tts(self, text: str):
        if not self.settings.get("tts_enabled", True) or not text.strip():
            return

        async def play_task(text_to_play, prev_task):
            try:
                if prev_task:
                    try:
                        await prev_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        logger.warning(f"Previous TTS task failed: {e}")

                ans_clean = discord.utils.remove_markdown(text_to_play)
                ans_clean = re.sub(r"●\s*[a-zA-Z0-9_]+\(.*?\)(.*?)(?=\n\n|\Z)", "", ans_clean, flags=re.DOTALL)
                ans_clean = re.sub(r"AbsolutePath:.*?(?=\n|$)", "", ans_clean)
                if ans_clean.strip() and self.guild_id and self.cog:
                    audio = await self.cog.tts(ans_clean)
                    if audio:
                        await self.cog._play_audio(self.guild_id, audio)
            except asyncio.CancelledError:
                logger.debug("TTS task was cancelled.")
                raise
            except Exception as e:
                logger.exception(f"TTS Streaming Error: {e}")

        prev = session_manager.get_tts_task(self.thread_id)
        new_task = asyncio.create_task(play_task(text, prev))
        session_manager.set_tts_task(self.thread_id, new_task)


async def stream_thinking_latest(bot, thread: Any, thread_id: str, context_dict: dict, queue: asyncio.Queue):
    cog = bot.get_cog("VoiceCog") if bot else None
    is_voice = bool(
        cog
        and hasattr(thread, "guild")
        and str(thread.guild.id) in cog._voice_state
        and cog._voice_state[str(thread.guild.id)] == thread.id
    )

    ui_mgr = StreamUpdater(thread, thread_id, context_dict)
    tts_mgr = TTSStreamManager(thread_id, thread.guild.id if hasattr(thread, "guild") else None, cog, is_voice)

    try:
        while True:
            item = await queue.get()
            if item is None:
                break

            if isinstance(item, tuple) and item and item[0] == "__RUN_ORDERED__":
                _, send_coro_factory, done_future = item
                await ui_mgr.split()
                await tts_mgr.flush_all()
                try:
                    result = await send_coro_factory()
                    if not done_future.done():
                        done_future.set_result(result)
                except Exception as e:
                    logger.exception(f"Ordered send failed: {e}")
                    if not done_future.done():
                        done_future.set_exception(e)
                continue

            chunk, force_flush = (item, False) if not isinstance(item, tuple) else item

            if chunk == "__END__":
                await ui_mgr.flush(force=True)
                if context_dict is not None:
                    context_dict["final_text"] = ui_mgr.current_text.strip()
                session_manager.remove_queue(thread_id)
                _clear_current_tool(thread_id)
                await tts_mgr.flush_all()
                break

            if str(chunk).startswith("__CONV_ID__:"):
                conv_id = chunk.split(":", 1)[1]
                session_manager.update_session(thread_id, "conversation_id", conv_id)
                continue

            if chunk == "__SPLIT__":
                await ui_mgr.split()
                await tts_mgr.flush_all()
                continue

            chunk = clean_ansi(chunk)

            await ui_mgr.process_chunk(chunk)
            await ui_mgr.flush()
            await tts_mgr.process_chunk(chunk)
    except asyncio.CancelledError:
        logger.debug(f"stream_thinking_latest cancelled for thread {thread_id}")
    except Exception as e:
        logger.exception(f"Error in stream_thinking_latest: {e}")
