"""Wake-word enrollment: /sound's recording flow to the confirmed .rpw reference."""

import asyncio
import io
import re
import time
import wave
from array import array as pyarray

import aiohttp
import discord
from discord.ext import tasks

NODE_VOICE_API = "http://localhost:18081"
# Default aiohttp timeout is 5 minutes - too long for a dead voice service.
NODE_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5)


class RecordingPromptView(discord.ui.View):
    def __init__(self, manager: "EnrollmentManager", user_id: str):
        super().__init__(timeout=120)
        self.manager = manager
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This isn't your recording session.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = self.manager._enrollment.get(self.user_id)
        word = session["word"] if session else "?"
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"❌ Cancelled wake-word setup for **{word}** - nothing was saved.", view=self
        )
        await self.manager.cancel_enrollment(self.user_id, edit_status=False)

    async def on_timeout(self):
        # cleanup_stale_enrollments handles the actual expiry/status edit.
        for child in self.children:
            child.disabled = True


class SampleConfirmView(discord.ui.View):
    def __init__(self, manager: "EnrollmentManager", user_id: str):
        super().__init__(timeout=60)
        self.manager = manager
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This isn't your recording session.", ephemeral=True)
            return False
        return True

    async def _disable(self, interaction: discord.Interaction, content: str | None = None):
        self.stop()
        for child in self.children:
            child.disabled = True
        if content is not None:
            await interaction.response.edit_message(content=content, view=self)
        else:
            await interaction.response.edit_message(view=self)

    @discord.ui.button(label="✅ Keep", style=discord.ButtonStyle.success)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._disable(interaction)
        await self.manager.resolve_sample_confirmation(self.user_id, keep=True)

    @discord.ui.button(label="🔁 Re-record", style=discord.ButtonStyle.secondary)
    async def redo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._disable(interaction)
        await self.manager.resolve_sample_confirmation(self.user_id, keep=False)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = self.manager._enrollment.get(self.user_id)
        word = session["word"] if session else "?"
        await self._disable(interaction, content=f"❌ Cancelled wake-word setup for **{word}** - nothing was saved.")
        await self.manager.cancel_enrollment(self.user_id, edit_status=False)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        await self.manager.resolve_sample_confirmation(self.user_id, keep=False, timed_out=True)


class EnrollmentManager:
    """Owns /sound's recording flow - see _commit_enrollment."""

    def __init__(self, bot, voice_state: dict, play_audio, bot_settings: dict, save_bot_settings, logger):
        self.bot = bot
        self._voice_state = voice_state  # shared with VoiceCog
        self._play_audio = play_audio
        self.bot_settings = bot_settings
        self.save_bot_settings = save_bot_settings
        self.logger = logger
        # user_id -> {word, accepted_samples, pending_sample,
        # awaiting_confirmation, needed, guild_id, started_at,
        # last_activity, status_msg}
        self._enrollment = {}
        self.cleanup_stale_enrollments.start()

    def stop(self):
        self.cleanup_stale_enrollments.cancel()

    def is_enrolling(self, user_id: str) -> bool:
        return user_id in self._enrollment

    async def handle_voice_service_down(self):
        """Called when Node dies."""
        stale_user_ids = list(self._enrollment.keys())
        for uid in stale_user_ids:
            session = self._enrollment.pop(uid, None)
            if not session:
                continue
            await self._update_status(
                session,
                f"🔌 Lost the connection to the voice service mid-recording for **{session['word']}** - "
                f"nothing was saved. Run `/sound` again once I'm back.",
            )

    @tasks.loop(seconds=30)
    async def cleanup_stale_enrollments(self):
        now = time.time()
        stale_user_ids = [uid for uid, s in self._enrollment.items() if now - s["last_activity"] > 120]
        for uid in stale_user_ids:
            session = self._enrollment.pop(uid, None)
            if not session:
                continue
            try:
                async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as http:
                    await http.post(f"{NODE_VOICE_API}/enroll_stop", json={"user_id": uid})
            except aiohttp.ClientError as e:
                self.logger.warning(f"Failed to stop stale enrollment forwarding for {uid}: {e}")
            await self._update_status(
                session,
                f"⌛ Wake-word recording for `{session['word']}` timed out - "
                f"nothing was saved. Run `/sound wake_word:...` again if you want to retry.",
            )

    async def cancel_enrollment(self, user_id: str, edit_status: bool = True):
        """edit_status=False when the caller already edited the message itself."""
        session = self._enrollment.pop(user_id, None)
        if not session:
            return
        try:
            async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as http:
                await http.post(f"{NODE_VOICE_API}/enroll_stop", json={"user_id": user_id})
        except aiohttp.ClientError as e:
            self.logger.warning(f"Failed to stop enrollment forwarding for {user_id}: {e}")
        if edit_status:
            await self._update_status(
                session, f"❌ Cancelled wake-word setup for **{session['word']}** - nothing was saved."
            )

    async def _update_status(self, session: dict, content: str, view: discord.ui.View | None = None):
        msg = session.get("status_msg")
        if msg:
            try:
                await msg.edit(content=content, view=view)
                return
            except discord.NotFound:
                session["status_msg"] = None
            except discord.HTTPException as e:
                self.logger.warning(f"Failed to edit enrollment status message: {e}")
                return

        thread_id = self._voice_state.get(session["guild_id"])
        thread = self.bot.get_channel(int(thread_id)) if thread_id else None
        if not thread:
            return
        try:
            session["status_msg"] = await thread.send(content, view=view)
        except discord.HTTPException as e:
            self.logger.warning(f"Failed to send enrollment status message: {e}")

    async def start_wake_word_recording(self, interaction: discord.Interaction, word: str) -> bool:
        """Called by /sound's wake_word param."""
        guild_id = interaction.guild_id
        user_id = str(interaction.user.id)

        if not self._voice_state.get(str(guild_id)):
            await interaction.response.send_message(
                "❌ Use `/join` first so I'm listening in this channel before setting a wake word.",
                ephemeral=True,
            )
            return False
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ Join a voice channel first - the wake word is recorded in your own voice.", ephemeral=True
            )
            return False
        if user_id in self._enrollment:
            await interaction.response.send_message(
                "⚠️ You already have a wake-word recording in progress - say the word, or cancel it first.",
                ephemeral=True,
            )
            return False

        word_clean = word.strip()
        if not word_clean:
            await interaction.response.send_message("❌ Give me an actual word.", ephemeral=True)
            return False

        session = {
            "word": word_clean,
            "accepted_samples": [],
            "pending_sample": None,
            "awaiting_confirmation": False,
            "needed": 5,
            "guild_id": str(guild_id),
            "started_at": time.time(),
            "last_activity": time.time(),
            "status_msg": None,
        }
        self._enrollment[user_id] = session
        try:
            async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as session_http:
                await session_http.post(f"{NODE_VOICE_API}/enroll_start", json={"user_id": user_id})
        except aiohttp.ClientError as e:
            del self._enrollment[user_id]
            await interaction.response.send_message(f"⚠️ Couldn't reach the voice service: {e}", ephemeral=True)
            return False

        await interaction.response.send_message(
            f"🎙️ **{interaction.user.display_name}** is setting the wake word to **{word_clean}**.\n"
            f"I'll play back each recording so you can re-record it if it's noisy. "
            f"Need 5 confirmed samples; nothing is saved until then."
        )
        await self._update_status(
            session,
            f"🎙️ **1/5** - say **{word_clean}** now.",
            view=RecordingPromptView(self, user_id),
        )
        return True

    async def handle_enroll_sample(self, user_id: str, audio_bytes: bytes):
        """Called via /enroll_sample for each captured sample."""
        session = self._enrollment.get(user_id)
        if not session:
            return  # stray sample - recording already finished/cancelled/expired

        if session.get("awaiting_confirmation"):
            return  # they're mid-playback/button-prompt for a previous sample - ignore stray audio

        session["last_activity"] = time.time()
        step = len(session["accepted_samples"]) + 1

        if len(audio_bytes) < 8000:
            await self._update_status(
                session,
                f"🎙️ **{step}/{session['needed']}** - didn't catch that clearly, say **{session['word']}** again.",
                view=RecordingPromptView(self, user_id),
            )
            return

        session["pending_sample"] = audio_bytes
        session["awaiting_confirmation"] = True

        await self._play_audio(session["guild_id"], audio_bytes, suppress_active_window=True)
        await self._update_status(
            session,
            f"🔊 **{step}/{session['needed']}** captured - keep it, or re-record if it's noisy?",
            view=SampleConfirmView(self, user_id),
        )

    async def resolve_sample_confirmation(self, user_id: str, keep: bool, timed_out: bool = False):
        session = self._enrollment.get(user_id)
        if not session:
            return
        session["last_activity"] = time.time()
        pending_sample = session.pop("pending_sample", None)
        session["awaiting_confirmation"] = False
        step = len(session["accepted_samples"]) + 1

        if timed_out:
            await self._update_status(
                session,
                f"⌛ **{step}/{session['needed']}** - no response, say **{session['word']}** again.",
                view=RecordingPromptView(self, user_id),
            )
            return

        if not keep or not pending_sample:
            await self._update_status(
                session,
                f"🔁 **{step}/{session['needed']}** discarded - say **{session['word']}** again.",
                view=RecordingPromptView(self, user_id),
            )
            return

        session["accepted_samples"].append(pending_sample)
        collected = len(session["accepted_samples"])
        needed = session["needed"]

        if collected < needed:
            await self._update_status(
                session,
                f"✅ **{collected}/{needed}** saved!\n🎙️ **{collected + 1}/{needed}** - say **{session['word']}** now.",
                view=RecordingPromptView(self, user_id),
            )
            return

        await self._update_status(session, f"✅ **{needed}/{needed}** saved! Building voice reference...")
        await self._commit_enrollment(user_id, session)

    async def _commit_enrollment(self, user_id: str, session: dict):
        from config import WAKE_REF_DIR

        user_dir = WAKE_REF_DIR / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        safe_word = re.sub(r"[^\w가-힣]", "_", session["word"])

        for stale in user_dir.glob("*.rpw"):  # drop old reference before rebuilding
            stale.unlink(missing_ok=True)

        wav_paths = []
        for i, sample_bytes in enumerate(session["accepted_samples"], start=1):
            wav_path = user_dir / f"{safe_word}-{i}.wav"
            wav_path.write_bytes(self._trim_silence_wav(sample_bytes))
            wav_paths.append(wav_path)

        rpw_path = user_dir / f"{safe_word}.rpw"
        build_ok = await self._build_rustpotter_reference(session["word"], rpw_path, wav_paths)

        if build_ok:
            # Node caches the detector per user_id - invalidate or it scores stale data.
            try:
                async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as http:
                    await http.post(f"{NODE_VOICE_API}/invalidate_detector", json={"user_id": user_id})
            except aiohttp.ClientError as e:
                self.logger.warning(f"Failed to invalidate cached detector for {user_id}: {e}")

        self.bot_settings["wake_words"] = session["word"]
        self.save_bot_settings(self.bot_settings)

        del self._enrollment[user_id]
        try:
            async with aiohttp.ClientSession(timeout=NODE_REQUEST_TIMEOUT) as http:
                await http.post(f"{NODE_VOICE_API}/enroll_stop", json={"user_id": user_id})
        except aiohttp.ClientError as e:
            self.logger.warning(f"Failed to stop enrollment forwarding for {user_id}: {e}")

        if build_ok:
            await self._update_status(session, f"✅ Wake word **{session['word']}** is registered to your voice.")
        else:
            await self._update_status(
                session,
                f"⚠️ Wake word set to **{session['word']}**, but I couldn't build the voice-matching "
                f"reference - voice wake-up won't work until this is fixed. "
                f"Check the logs for details, then run `/sound` again.",
            )

    def _trim_silence_wav(self, wav_bytes: bytes, threshold: int = 400, margin_sec: float = 0.05) -> bytes:
        """Trims trailing silence (recordings end ~800ms after speech
        stops) - untrimmed, rustpotter's sustain-duration check never
        fires in time. Falls back to the original bytes on any failure."""
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
                channels = reader.getnchannels()
                sample_width = reader.getsampwidth()
                frame_rate = reader.getframerate()
                raw = reader.readframes(reader.getnframes())

            if sample_width != 2:
                return wav_bytes  # not 16-bit PCM

            samples = pyarray("h")
            samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
            total_frames = len(samples) // channels if channels else 0
            if total_frames == 0:
                return wav_bytes

            def frame_amplitude(frame_index: int) -> int:
                base = frame_index * channels
                return max(abs(samples[base + c]) for c in range(channels))

            start = 0
            while start < total_frames and frame_amplitude(start) < threshold:
                start += 1
            end = total_frames
            while end > start and frame_amplitude(end - 1) < threshold:
                end -= 1

            margin = int(margin_sec * frame_rate)
            start = max(0, start - margin)
            end = min(total_frames, end + margin)

            if (end - start) < int(0.1 * frame_rate):  # near-silent clip - keep original
                return wav_bytes

            trimmed = samples[start * channels : end * channels]
            out = io.BytesIO()
            with wave.open(out, "wb") as writer:
                writer.setnchannels(channels)
                writer.setsampwidth(sample_width)
                writer.setframerate(frame_rate)
                writer.writeframes(trimmed.tobytes())
            return out.getvalue()
        except Exception as e:
            self.logger.warning(f"Silence trim failed, using untrimmed sample: {e}")
            return wav_bytes

    async def _build_rustpotter_reference(self, word: str, rpw_path, wav_paths: list) -> bool:
        import base64

        samples = [
            {"filename": p.name, "data_base64": base64.b64encode(p.read_bytes()).decode("ascii")} for p in wav_paths
        ]

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
                resp = await http.post(f"{NODE_VOICE_API}/build_wakeword", json={"name": word, "samples": samples})
                data = await resp.json()
        except aiohttp.ClientError as e:
            self.logger.error(f"Failed to reach voice service to build wakeword reference for '{word}': {e}")
            return False
        except asyncio.TimeoutError:
            self.logger.error(f"Timed out building wakeword reference for '{word}'")
            return False

        if not data.get("success"):
            self.logger.error(f"Failed to build wakeword reference for '{word}': {data.get('error')}")
            return False

        rpw_path.write_bytes(base64.b64decode(data["rpw_base64"]))
        self.logger.info(f"Built {rpw_path.name} for '{word}' via voice-service's WakewordRefCreator.")
        return True
