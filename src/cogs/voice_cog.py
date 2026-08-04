import asyncio
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import allowed, logger
from messengers.registry import get_adapter_for_platform

from .voice.enrollment import EnrollmentManager
from .voice.stt_session import SttSessionTracker

NODE_VOICE_API = "http://localhost:18081"
# Default aiohttp timeout is 5 minutes - too long for a dead voice service.
NODE_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5)


class VoiceCog(commands.Cog):
    def __init__(
        self,
        bot,
        stt,
        tts,
        send_agy_response,
        agy_send,
        stream_thinking_latest,
        agy_start_session,
        session_manager,
        bot_settings,
        save_bot_settings,
        logger,
    ):
        self.bot = bot
        # Unused by the voice pipeline now (STT moved to Node); kept for other callers.
        self.stt = stt
        self.tts = tts
        self.send_agy_response = send_agy_response
        self.agy_send = agy_send
        self.stream_thinking_latest = stream_thinking_latest
        self.agy_start_session = agy_start_session
        self.session_manager = session_manager
        self.bot_settings = bot_settings
        self.save_bot_settings = save_bot_settings
        self.logger = logger
        self._voice_state = {}
        # guild_id -> in-flight handle_stt_input task; a new utterance cancels the previous one.
        self._active_turns = {}
        self.enrollment = EnrollmentManager(
            bot, self._voice_state, self._play_audio, self.bot_settings, self.save_bot_settings, self.logger
        )
        self.stt_session = SttSessionTracker(bot, self._voice_state, self.bot_settings, self.logger)
        self.cleanup_old_voice_files.start()

    def cog_unload(self):
        self.cleanup_old_voice_files.cancel()
        self.enrollment.stop()

    def _wake_word_required(self, user_id) -> bool:
        return (self.bot_settings.get("wake_word_required") or {}).get(str(user_id), True)

    async def handle_voice_service_down(self):
        await self.enrollment.handle_voice_service_down()

    @tasks.loop(minutes=5)
    async def cleanup_old_voice_files(self):
        try:
            from config import TMP_VOICE_DIR

            now = time.time()
            count = 0
            for f in TMP_VOICE_DIR.glob("*.mp3"):
                if now - f.stat().st_mtime > 600:
                    f.unlink(missing_ok=True)
                    count += 1
            if count > 0:
                self.logger.debug(f"Garbage Collector: Deleted {count} old TTS audio files.")
        except Exception as e:
            self.logger.error(f"Garbage Collector error: {e}")

    async def active_times_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        current_val = int(self.bot_settings.get("active_timer", 60))
        opts = []
        if str(current_val) in current or not current:
            opts.append(app_commands.Choice(name=f"{current_val} (current)", value=current_val))

        for v in [30, 60, 120, 300]:
            if v != current_val and len(opts) < 25:
                opts.append(app_commands.Choice(name=str(v), value=v))
        return opts

    async def interrupt_threshold_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        current_val = int(self.bot_settings.get("voice_threshold", 3000))
        opts = []
        if str(current_val) in current or not current:
            opts.append(app_commands.Choice(name=f"{current_val} (current)", value=current_val))

        for v in [1000, 2000, 3000, 5000]:
            if v != current_val and len(opts) < 25:
                opts.append(app_commands.Choice(name=str(v), value=v))
        return opts

    async def wake_sensitivity_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[float]]:
        current_val = float(self.bot_settings.get("wake_threshold", 0.4))
        opts = []
        if str(current_val) in current or not current:
            opts.append(app_commands.Choice(name=f"{current_val} (current)", value=current_val))

        for v in [0.2, 0.3, 0.4, 0.5, 0.6]:
            if v != current_val and len(opts) < 25:
                opts.append(app_commands.Choice(name=str(v), value=v))
        return opts

    async def tts_voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current_val = self.bot_settings.get("tts_voice", "en-US-AriaNeural")
        options = [
            "en-US-AriaNeural",
            "en-US-GuyNeural",
            "en-US-AnaNeural",
            "en-US-ChristopherNeural",
            "en-US-EricNeural",
            "en-US-MichelleNeural",
            "en-US-RogerNeural",
            "en-GB-SoniaNeural",
            "en-GB-RyanNeural",
            "en-AU-NatashaNeural",
            "en-AU-WilliamNeural",
            "ko-KR-SunHiNeural",
            "ko-KR-InJoonNeural",
            "ja-JP-NanamiNeural",
            "ja-JP-KeitaNeural",
            "fr-FR-DeniseNeural",
            "de-DE-KatjaNeural",
            "es-ES-ElviraNeural",
        ]

        choices = []
        if current.lower() in current_val.lower() or not current:
            choices.append(app_commands.Choice(name=f"{current_val} (current)", value=current_val))

        for opt in options:
            if current.lower() in opt.lower() and opt != current_val and len(choices) < 25:
                choices.append(app_commands.Choice(name=opt, value=opt))
        return choices

    async def tts_enabled_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        is_on = self.bot_settings.get("tts_enabled", True)
        current_val = "ON" if is_on else "OFF"
        other_val = "OFF" if is_on else "ON"

        opts = []
        if current.lower() in current_val.lower() or not current:
            opts.append(app_commands.Choice(name=f"{current_val} (current)", value=current_val.lower()))
        if current.lower() in other_val.lower():
            opts.append(app_commands.Choice(name=other_val, value=other_val.lower()))
        return opts

    async def require_wake_word_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        is_on = self._wake_word_required(interaction.user.id)
        current_val = "ON" if is_on else "OFF"
        other_val = "OFF" if is_on else "ON"

        opts = []
        if current.lower() in current_val.lower() or not current:
            opts.append(app_commands.Choice(name=f"{current_val} (current)", value=current_val.lower()))
        if current.lower() in other_val.lower():
            opts.append(app_commands.Choice(name=other_val, value=other_val.lower()))
        return opts

    async def tts_speed_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[float]]:
        current_val = float(self.bot_settings.get("tts_speed", 1.0))
        opts = []
        if str(current_val) in current or not current:
            opts.append(app_commands.Choice(name=f"{current_val}x (current)", value=current_val))

        for v in [0.75, 1.0, 1.25, 1.3, 1.5, 1.75, 2.0]:
            if v != current_val and len(opts) < 25:
                opts.append(app_commands.Choice(name=f"{v}x", value=v))
        return opts

    @app_commands.command(name="join", description="Summon the bot to your current voice channel")
    async def cmd_join(self, interaction: discord.Interaction):
        if not allowed(interaction.user.id):
            await interaction.response.send_message("❌ Permission Denied", ephemeral=True)
            return

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "❌ This command can only be used inside a thread created with `/new`.", ephemeral=True
            )
            return

        if not self.session_manager.get_session(str(interaction.channel_id)):
            await interaction.response.send_message(
                "❌ This thread is not an active agy session. Use `/new` to start a new session.", ephemeral=True
            )
            return

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ Please join a voice channel first.", ephemeral=True)
            return

        voice_channel = interaction.user.voice.channel
        guild_id = interaction.guild_id

        wake_word_map = self.bot_settings.get("wake_words") or {}
        own_word = wake_word_map.get(str(interaction.user.id))
        active_timer = self.bot_settings.get("active_timer", 60)
        required = self._wake_word_required(interaction.user.id)

        header = f"🎤 Connected to `{voice_channel.name}`."
        sound_tip = "⚙️ You can customize settings using `/sound`."

        if not required:
            body = "💡 Wake word is off for you - just talk, I'm listening."
        elif own_word:
            body = (
                f"💡 Say `{own_word}` to activate me. Once awake, "
                f"I'll keep listening for {active_timer} seconds after each interaction."
            )
        else:
            body = (
                "🎙️ You haven't set up a wake word yet, so I can't hear you - run `/sound wake_word:<word>` "
                "and say your chosen word a few times to register it in your voice.\n"
                "💡 A wake word keeps everyone else's side conversation from triggering me by accident, and "
                "avoids running speech recognition on audio that isn't meant for me. If you use push-to-talk, "
                "turning it off with `/sound require_wake_word:off` is recommended instead."
            )

        msg = "\n".join([header, body, sound_tip])
        await interaction.response.send_message(msg)

        self.stt_session.clear_active_window(str(guild_id))  # don't inherit a window left open by a prior /join

        try:
            async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as session:
                try:
                    await session.post(f"{NODE_VOICE_API}/leave", json={"guild_id": str(guild_id)})
                except Exception as e:
                    self.logger.warning(f"Failed to call /leave before /join: {e}")

                resp = await session.post(
                    f"{NODE_VOICE_API}/join", json={"guild_id": str(guild_id), "channel_id": str(voice_channel.id)}
                )
                if not required:
                    # Node's opt-out set is in-memory and won't survive a Node restart, unlike our own bot_settings.
                    await session.post(
                        f"{NODE_VOICE_API}/set_wake_word_required",
                        json={"user_id": str(interaction.user.id), "required": False},
                    )
                data = await resp.json()
                if data.get("success"):
                    self._voice_state[str(guild_id)] = interaction.channel_id

                    if own_word or not required:
                        if self.bot_settings.get("tts_enabled", True):
                            welcome_audio = await self.tts("Voice connected.")
                            if welcome_audio:
                                await self._play_audio(str(guild_id), welcome_audio, suppress_active_window=True)
                    elif self.bot_settings.get("tts_enabled", True):
                        prompt_audio = await self.tts(
                            "No wake word is set up yet. Please use the sound command to set one."
                        )
                        if prompt_audio:
                            await self._play_audio(str(guild_id), prompt_audio, suppress_active_window=True)
                else:
                    await interaction.channel.send(f"⚠️ Node.js integration failed: {data.get('error')}")
        except aiohttp.ClientConnectorError:
            self.logger.error("Voice service is unreachable (not started yet, or crashed)")
            await interaction.channel.send(
                "⚠️ The voice service isn't reachable right now - it may still be starting up "
                "(wait a few seconds and try `/join` again), or it may have crashed (check `lgy logs`)."
            )
        except aiohttp.ClientError as e:
            self.logger.error(f"Node.js connection error in /join: {e}")
            await interaction.channel.send(f"⚠️ Node.js connection error: {e}")
        except asyncio.TimeoutError:
            self.logger.error("Timeout connecting to Node.js backend")
            await interaction.channel.send("⚠️ Timeout connecting to voice backend.")

    @app_commands.command(
        name="sound",
        description="Configure voice settings (Wake word, active time, thresholds, TTS voice/speed, TTS on/off)",
    )
    @app_commands.describe(
        wake_word="The single word/phrase that wakes the bot (recorded in your voice)",
        active_times="Duration in seconds the bot stays awake",
        interrupt_threshold="Mic volume that interrupts (barges into) TTS playback (1000~10000)",
        wake_sensitivity="Wake word match sensitivity (0.1~0.9, lower = easier to trigger but more false wakes)",
        tts_voice="Select the AI TTS voice",
        tts_enabled="Turn Text-to-Speech ON or OFF",
        tts_speed="TTS playback speed multiplier, e.g. 1.3 for 1.3x (0.5~2.0)",
        require_wake_word="Require your wake word before I listen (default ON) - turn OFF if you use push-to-talk",
    )
    @app_commands.autocomplete(
        active_times=active_times_autocomplete,
        interrupt_threshold=interrupt_threshold_autocomplete,
        wake_sensitivity=wake_sensitivity_autocomplete,
        tts_voice=tts_voice_autocomplete,
        tts_enabled=tts_enabled_autocomplete,
        tts_speed=tts_speed_autocomplete,
        require_wake_word=require_wake_word_autocomplete,
    )
    async def cmd_voice(
        self,
        interaction: discord.Interaction,
        wake_word: str = None,
        active_times: int = None,
        interrupt_threshold: int = None,
        wake_sensitivity: float = None,
        tts_voice: str = None,
        tts_enabled: str = None,
        tts_speed: float = None,
        require_wake_word: str = None,
    ):
        import aiohttp

        if not allowed(interaction.user.id):
            await interaction.response.send_message("❌ Permission Denied", ephemeral=True)
            return

        if (
            wake_word is None
            and active_times is None
            and interrupt_threshold is None
            and wake_sensitivity is None
            and tts_voice is None
            and tts_enabled is None
            and tts_speed is None
            and require_wake_word is None
        ):
            curr_wake = (self.bot_settings.get("wake_words") or {}).get(str(interaction.user.id), "None")
            curr_timer = self.bot_settings.get("active_timer", 60)
            curr_interrupt_thresh = self.bot_settings.get("voice_threshold", 3000)
            curr_wake_sens = self.bot_settings.get("wake_threshold", 0.4)
            curr_tts = self.bot_settings.get("tts_voice", "en-US-AriaNeural")
            curr_tts_on = "ON" if self.bot_settings.get("tts_enabled", True) else "OFF"
            curr_tts_speed = self.bot_settings.get("tts_speed", 1.0)
            curr_required = self._wake_word_required(interaction.user.id)

            embed = discord.Embed(title="⚙️ Current Voice Settings", color=0x3498DB)
            embed.add_field(name="🎙️ Wake Word", value=f"`{curr_wake}`", inline=False)
            embed.add_field(name="🔒 Wake Word Required", value=f"`{'ON' if curr_required else 'OFF'}`", inline=False)
            embed.add_field(name="⏱️ Active Time", value=f"`{curr_timer}s`", inline=False)
            embed.add_field(name="🎯 Wake Sensitivity", value=f"`{curr_wake_sens}`", inline=False)
            embed.add_field(name="🔊 Interrupt Threshold", value=f"`{curr_interrupt_thresh}`", inline=False)
            embed.add_field(name="🗣️ TTS Voice", value=f"`{curr_tts}`", inline=False)
            embed.add_field(name="🔊 TTS Enabled", value=f"`{curr_tts_on}`", inline=False)
            embed.add_field(name="⏩ TTS Speed", value=f"`{curr_tts_speed}x`", inline=False)
            return await interaction.response.send_message(embed=embed)

        updated = []
        wake_word_pending = False
        if wake_word is not None:
            # Deferred - wake_words is set later once 5 samples are recorded (see EnrollmentManager).
            wake_word_pending = True
        if active_times is not None:
            self.bot_settings["active_timer"] = active_times
            updated.append(f"⏱️ Active Timer: `{active_times}s`")
        if interrupt_threshold is not None:
            self.bot_settings["voice_threshold"] = interrupt_threshold
            updated.append(f"🔊 Interrupt Threshold: `{interrupt_threshold}`")
            try:
                async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as session:
                    await session.post(f"{NODE_VOICE_API}/set_config", json={"voice_threshold": interrupt_threshold})
            except aiohttp.ClientError as e:
                self.logger.warning(f"Node.js sync failed for {interaction.guild_id}: {e}")
                updated.append(f"(⚠️ Node.js Sync Failed: {e})")
            except asyncio.TimeoutError:
                self.logger.warning(f"Node.js sync timeout for {interaction.guild_id}")
                updated.append("(⚠️ Node.js Sync Timeout)")
        if wake_sensitivity is not None:
            clamped_wake = max(0.05, min(0.95, wake_sensitivity))
            self.bot_settings["wake_threshold"] = clamped_wake
            updated.append(f"🎯 Wake Sensitivity: `{clamped_wake}`")
            try:
                async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as session:
                    await session.post(f"{NODE_VOICE_API}/set_config", json={"wake_threshold": clamped_wake})
            except aiohttp.ClientError as e:
                self.logger.warning(f"Node.js sync failed for {interaction.guild_id}: {e}")
                updated.append(f"(⚠️ Node.js Sync Failed: {e})")
            except asyncio.TimeoutError:
                self.logger.warning(f"Node.js sync timeout for {interaction.guild_id}")
                updated.append("(⚠️ Node.js Sync Timeout)")
        if tts_voice is not None:
            self.bot_settings["tts_voice"] = tts_voice
            updated.append(f"🗣️ TTS Voice: `{tts_voice}`")
        if tts_enabled is not None:
            is_on = tts_enabled.lower() == "on"
            self.bot_settings["tts_enabled"] = is_on
            updated.append(f"🔊 TTS Enabled: `{'ON' if is_on else 'OFF'}`")
        if tts_speed is not None:
            clamped = max(0.5, min(2.0, tts_speed))
            self.bot_settings["tts_speed"] = clamped
            updated.append(f"⏩ TTS Speed: `{clamped}x`")
        if require_wake_word is not None:
            is_required = require_wake_word.lower() != "off"
            required_map = self.bot_settings.setdefault("wake_word_required", {})
            required_map[str(interaction.user.id)] = is_required
            updated.append(f"🔒 Wake Word Required: `{'ON' if is_required else 'OFF'}`")
            try:
                async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as session:
                    await session.post(
                        f"{NODE_VOICE_API}/set_wake_word_required",
                        json={"user_id": str(interaction.user.id), "required": is_required},
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.logger.warning(f"Node.js wake-word-required sync failed for {interaction.user.id}: {e}")
                updated.append(f"(⚠️ Node.js Sync Failed: {e})")

        self.save_bot_settings(self.bot_settings)

        if wake_word_pending:
            # start_wake_word_recording owns the interaction reply (error or "say it now").
            started = await self.enrollment.start_wake_word_recording(interaction, wake_word)
            if updated:
                embed = discord.Embed(
                    title="⚙️ Other Voice Settings Updated", description="\n".join(updated), color=0x3498DB
                )
                if started:
                    await interaction.channel.send(embed=embed)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(title="⚙️ Voice Settings Updated", description="\n".join(updated), color=0x3498DB)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Make the bot leave the voice channel")
    async def cmd_leave(self, interaction: discord.Interaction):
        if not allowed(interaction.user.id):
            await interaction.response.send_message("❌ Permission Denied", ephemeral=True)
            return

        guild_id = interaction.guild_id
        await interaction.response.send_message("👋 Disconnected from voice channel.")
        try:
            async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as session:
                await session.post(f"{NODE_VOICE_API}/leave", json={"guild_id": str(guild_id)})
            if str(guild_id) in self._voice_state:
                del self._voice_state[str(guild_id)]
        except aiohttp.ClientError as e:
            self.logger.warning(f"Websocket connection closed with error: {e}")
        except asyncio.CancelledError:
            self.logger.debug("Websocket listener task cancelled.")

    async def handle_enroll_sample(self, user_id: str, audio_bytes: bytes):
        await self.enrollment.handle_enroll_sample(user_id, audio_bytes)

    async def _play_audio(self, guild_id: str, audio_bytes: bytes, suppress_active_window: bool = False):
        """Sends TTS bytes to Node directly (no temp file). suppress_active_window=True
        (enrollment playback) stops it from extending the awake window."""
        try:
            async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as session:
                await session.post(
                    f"{NODE_VOICE_API}/play",
                    params={"guild_id": guild_id, "suppress_active_window": str(suppress_active_window).lower()},
                    data=audio_bytes,
                    headers={"Content-Type": "application/octet-stream"},
                )
        except aiohttp.ClientError as e:
            self.logger.error(f"Node.js play error (network): {e}")
        except asyncio.TimeoutError:
            self.logger.error("Node.js play error: Timeout")

    async def handle_stt_input(self, data):
        try:
            guild_id = data.get("guild_id")
            user_id = data.get("user_id")

            if not allowed(int(user_id)):
                self.logger.debug(f"STT: ignoring speech from non-allowed user {user_id}")
                await self.stt_session.clear_partial_msg(str(guild_id))
                return

            # Node runs STT itself before this call, so 'text' is already-recognized, not raw audio.
            text = data.get("text")

            thread_id = self._voice_state.get(str(guild_id))
            self.logger.debug(f"STT handler: guild_id={guild_id}, thread_id={thread_id}")
            if not thread_id:
                self.logger.debug("STT: thread_id is None, skipping")
                return

            thread = self.bot.get_channel(int(thread_id))
            self.logger.debug(f"STT: thread={thread}")
            if not thread:
                self.logger.debug("STT: thread is None, skipping")
                return

            if not text:
                self.logger.debug("STT: silence/no speech detected, skipping")
                await self.stt_session.clear_partial_msg(str(guild_id))
                return

            import difflib
            import re

            # Wake detection is Node's Rustpotter detector's job - no text-similarity fallback.
            is_waking_up = bool(data.get("wake_confirmed"))
            matched_wake_word = data.get("matched_wake_word")
            is_active = self.stt_session.is_active(str(guild_id))

            if not is_active and not is_waking_up and self._wake_word_required(user_id):
                self.logger.debug(f"STT: ignored (sleeping): {text}")
                await self.stt_session.clear_partial_msg(str(guild_id))
                return

            text_to_ai = text
            prefix_similarity = None
            if matched_wake_word:
                pattern = re.compile(re.escape(matched_wake_word) + r"[아야]?\s*[^\w\s]*\s*", re.IGNORECASE)
                text_to_ai = pattern.sub("", text_to_ai, count=1).strip()

                if text_to_ai == text:
                    # Exact match failed (STT can mistranscribe); try a fuzzy phonetic prefix match.
                    words = text.split()
                    wake_word_count = max(1, len(matched_wake_word.split()))
                    if words:
                        prefix = re.sub(r"[^\w가-힣]", "", "".join(words[:wake_word_count]))
                        wake_clean = re.sub(r"[^\w가-힣]", "", matched_wake_word)
                        try:
                            from jamo import h2j, j2hcj

                            prefix_similarity = difflib.SequenceMatcher(
                                None, j2hcj(h2j(prefix)), j2hcj(h2j(wake_clean))
                            ).ratio()
                        except Exception:
                            prefix_similarity = difflib.SequenceMatcher(None, prefix, wake_clean).ratio()
                        if prefix_similarity >= 0.6:
                            text_to_ai = " ".join(words[wake_word_count:]).strip()

            # Short wake words have less acoustic signal (more false-wakes) -> need a closer match.
            wake_syllables = len(re.sub(r"[^\w가-힣]", "", matched_wake_word or ""))
            min_prefix_similarity = 0.55 if wake_syllables <= 2 else 0.35
            if is_waking_up and prefix_similarity is not None and prefix_similarity < min_prefix_similarity:
                self.logger.info(
                    f"STT: ignoring wake - '{text}' doesn't resemble '{matched_wake_word}' "
                    f"(prefix similarity {prefix_similarity:.2f}, needed {min_prefix_similarity:.2f})"
                )
                await self.stt_session.clear_partial_msg(str(guild_id))
                return

            self.logger.debug(f"STT recognized: {text} -> AI: {text_to_ai}")

            sess = self.session_manager.get_session(str(thread_id))
            if not sess:
                sess = {"status": "pending", "user_id": str(user_id)}
                self.session_manager.set_session(str(thread_id), sess)

            conv_id = sess.get("conversation_id")
            pa = self.session_manager.get_pending_approval_by_conv(conv_id) if conv_id else None
            has_pending_approval = bool(conv_id and pa and not pa.done())

            # Cancel a stale in-flight turn, unless a tool/question approval is pending (this utterance may be its answer).
            prev_task = self._active_turns.get(str(guild_id))
            if prev_task and not prev_task.done() and not has_pending_approval:
                prev_task.cancel()

                from core.agy_runner import stop_active_process

                stop_active_process(str(thread_id))

                prev_session = self.session_manager.get_session(str(thread_id))
                if prev_session:
                    self.session_manager.set_session(
                        str(thread_id), {**prev_session, "status": "pending", "conversation_id": None}
                    )

                try:
                    async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as session:
                        await session.post(f"{NODE_VOICE_API}/interrupt", json={"guild_id": guild_id})
                except aiohttp.ClientError as e:
                    self.logger.warning(f"Failed to interrupt playback for guild {guild_id}: {e}")
            self._active_turns[str(guild_id)] = asyncio.current_task()

            guild = self.bot.get_guild(int(guild_id))
            member = guild.get_member(int(user_id)) if guild else None
            if guild and not member:
                try:
                    member = await guild.fetch_member(int(user_id))
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    self.logger.warning(f"fetch_member failed for {user_id}: {e}")
            username = member.display_name if member else f"User {user_id}"
            await self.stt_session.finalize_partial_msg(str(guild_id), thread, f"🎤 **{username}**: {text}")

            if not text_to_ai:
                self.logger.debug("STT: isolated wake word handled via direct TTS")
                if self.bot_settings.get("tts_enabled", True):
                    audio_reply = await self.tts("Yes, I am listening.")
                    if audio_reply:
                        await self._play_audio(str(guild_id), audio_reply)
                return

            if has_pending_approval:
                app_type = self.session_manager.get_pending_approval_type_by_conv(conv_id)
                if app_type == "ask_question":
                    pa.set_result(text)
                    await thread.send(f'✅ *Voice Answer Received: "{text}"*')
                    return

                from services.discord_helpers import check_approval_intent

                intent = check_approval_intent(text)
                if intent == "allow":
                    pa.set_result("allow")
                    await thread.send("✅ *Voice Approval Received*")
                    return
                elif intent == "reject":
                    pa.set_result("reject")
                    await thread.send("❌ *Voice Rejection Received*")
                    return

            async with thread.typing():
                self.logger.debug(f"🎤 [{username} ({user_id})] said: {text}")

                sess = self.session_manager.get_session(str(thread_id))
                is_new_session = sess.get("status") == "pending" or not conv_id

                queue = asyncio.Queue()
                self.session_manager.register_queue(str(thread.id), queue)

                ctx = {"status_msg": None}
                consume_task = asyncio.create_task(self.stream_thinking_latest(thread, str(thread.id), ctx, queue))

                try:
                    if is_new_session:
                        raw_ans, new_conv_id = await self.agy_start_session(
                            text_to_ai,
                            model=sess.get("model"),
                            stream_queue=queue,
                            thread_id=str(thread_id),
                            cwd=sess.get("cwd"),
                        )
                        sess["conversation_id"] = new_conv_id
                        sess["status"] = "active"
                        self.session_manager.set_session(str(thread_id), sess)
                        conv_id = new_conv_id

                        from utils.utils import generate_thread_title, update_agy_conversation_title

                        if thread.name.startswith("Session-"):
                            new_title = await generate_thread_title(text_to_ai, raw_ans)
                            await get_adapter_for_platform("discord").rename_conversation(thread, new_title)
                            await update_agy_conversation_title(new_conv_id, new_title)
                    else:
                        logger.debug("Voice: calling agy_send...")
                        raw_ans = await self.agy_send(
                            conv_id, text_to_ai, model=sess.get("model"), thread_id=str(thread_id), stream_queue=queue
                        )
                        logger.debug("Voice: agy_send finished")

                    final_text = raw_ans
                finally:
                    logger.debug("Voice: sending __END__ and waiting for consume_task")
                    await queue.put(("__END__", True))
                    await consume_task

                logger.debug("Voice: calling send_agy_response...")
                response_text = ctx.get("final_text", final_text)
                await self.send_agy_response(
                    thread, response_text, sess, ctx=ctx, start_time=time.time(), conv_id=conv_id
                )
                logger.debug("Voice: send_agy_response finished")

                if not self.bot_settings.get("tts_enabled", True):
                    self.stt_session.extend_active_window(str(guild_id))

        except asyncio.CancelledError:
            self.logger.debug(f"handle_stt_input task cancelled for thread {thread_id}")
        except Exception as e:
            self.logger.exception(f"Unhandled error in handle_stt_input: {e}")

    def mark_tts_finished(self, guild_id: str):
        self.stt_session.mark_tts_finished(guild_id)

    async def handle_stt_partial(self, data: dict):
        await self.stt_session.handle_stt_partial(data)

    async def cancel_stt_partial(self, guild_id: str):
        await self.stt_session.cancel_stt_partial(guild_id)
