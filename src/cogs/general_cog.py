import asyncio
import glob
import os
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    DATA_DIR,
    EMBED_COLOR,
    MODEL_CHOICES,
    allowed,
    bot_settings,
    is_allowed_session_channel,
    logger,
    save_bot_settings,
    session_manager,
)
from core.atomic_io import atomic_write_json, safe_load_json
from utils.utils import get_default_cwd

MODELS_CACHE_FILE = DATA_DIR / "models.json"


def load_cached_models():
    default_models = [
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.5 Flash (High)",
        "Gemini 3.5 Flash (Low)",
        "Gemini 3.1 Pro (Low)",
        "Gemini 3.1 Pro (High)",
        "Claude Sonnet 4.6 (Thinking)",
        "Claude Opus 4.6 (Thinking)",
        "GPT-OSS 120B (Medium)",
    ]
    return safe_load_json(MODELS_CACHE_FILE, default_models, logger=logger)


cached_models = load_cached_models()
last_models_fetch = 0
fetching_models = False


async def fetch_models_background():
    global cached_models, last_models_fetch, fetching_models
    import re
    import time

    try:
        from config import AGY_BIN

        logger.debug(f"Starting fetch_models_background using AGY_BIN: {AGY_BIN}")
        env = os.environ.copy()
        env["LGY_APPROVAL_HOOK"] = "1"
        p = await asyncio.create_subprocess_exec(
            AGY_BIN,
            "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(p.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            p.kill()
            logger.error("agy models timed out after 30 seconds.")
            return

        raw_text = stdout.decode("utf-8")
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        raw_text = ansi_escape.sub("", raw_text)
        raw_text = raw_text.replace("\r", "\n")

        models = []
        for line in raw_text.split("\n"):
            line = line.strip()
            line = re.sub(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", "", line).strip()
            if line and "Fetching available models" not in line:
                models.append(line)

        if models:
            cached_models = models
            last_models_fetch = time.time()
            try:
                atomic_write_json(MODELS_CACHE_FILE, models)
            except Exception as e:
                logger.warning(f"Failed to save models to JSON cache: {e}")
            logger.debug(f"Successfully fetched {len(models)} models and saved to models.json")
        else:
            logger.warning(f"Failed to parse any models. Raw stdout was: {raw_text[:200]}")
    except Exception as e:
        logger.warning(f"Failed to fetch models natively: {e}")
    finally:
        fetching_models = False


class GeneralCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        asyncio.create_task(fetch_models_background())

    async def cwd_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not current:
            current = str(Path.home())
        try:
            paths = [d + "/" for d in glob.glob(current + "*") if os.path.isdir(d)]
            return [app_commands.Choice(name=p, value=p) for p in paths if p and len(p) <= 100][:25]
        except Exception:
            return []

    async def model_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        global cached_models, last_models_fetch, fetching_models
        import time

        cached_models = load_cached_models()

        if time.time() - last_models_fetch > 3600 and not fetching_models:
            fetching_models = True
            asyncio.create_task(fetch_models_background())

        from utils.utils import get_current_model

        session = session_manager.get_session(str(interaction.channel_id)) or {}
        current_model = session.get("model") or bot_settings.get("default_model") or get_current_model()

        choices = []
        if current_model:
            choices.append(app_commands.Choice(name=f"{current_model} (current)", value=current_model))

        for m in cached_models:
            if m != current_model and current.lower() in m.lower() and len(choices) < 25:
                choices.append(app_commands.Choice(name=m, value=m))
        return choices

    @app_commands.command(name="new", description="Start a new agy session thread (or session, in a DM)")
    @app_commands.describe(cwd="Working directory path", model="Model to use")
    async def cmd_new(self, interaction: discord.Interaction, cwd: str = None, model: str = None):
        if not allowed(interaction.user.id):
            await interaction.response.send_message("❌ Permission Denied", ephemeral=True)
            return

        if model:
            exact = next((m for m in cached_models if m.lower() == model.lower()), None)
            partial = next((m for m in cached_models if model.lower() in m.lower()), None)
            model = exact or partial
            if not model:
                await interaction.response.send_message(f"⚠️ Model matching `{model}` not found.", ephemeral=True)
                return
        else:
            model = bot_settings.get("default_model") or None

        # DMs have no threads, so the DM channel itself is the session (1 chat = 1 session,
        # same model as Telegram) - skip the guild-scoped channel check entirely.
        if interaction.guild is None:
            await self._start_dm_session(interaction, cwd, model)
            return

        target_channel = (
            interaction.channel.parent if isinstance(interaction.channel, discord.Thread) else interaction.channel
        )
        if not is_allowed_session_channel(target_channel):
            await interaction.response.send_message(
                "❌ This channel/server isn't configured for starting sessions. Run `lgy setup` to add it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        import uuid

        new_conv_id = str(uuid.uuid4())
        thread = await target_channel.create_thread(
            name=f"Session-{new_conv_id[:4].upper()}",
            auto_archive_duration=1440,
            type=discord.ChannelType.public_thread,
        )

        session_manager.set_session(
            str(thread.id),
            {
                "status": "pending",
                "platform": "discord",
                "user_id": interaction.user.id,
                "cwd": cwd or get_default_cwd(),
                "model": model,
                "conversation_id": None,
                "created_at": datetime.now().isoformat(),
            },
        )
        await thread.send("✅ **Ready for new session!**")
        await interaction.followup.send(f"✅ New session created: {thread.mention}", ephemeral=True)

    async def _start_dm_session(self, interaction: discord.Interaction, cwd: str, model: str) -> None:
        channel = interaction.channel
        conv_key = str(channel.id)

        def _create_session() -> None:
            session_manager.set_session(
                conv_key,
                {
                    "status": "pending",
                    "platform": "discord",
                    "user_id": interaction.user.id,
                    "cwd": cwd or get_default_cwd(),
                    "model": model,
                    "conversation_id": None,
                    "created_at": datetime.now().isoformat(),
                },
            )

        if not session_manager.get_session(conv_key):
            _create_session()
            await interaction.response.send_message("✅ **Ready for new session!** Send a message to begin.")
            return

        # Existing session found: same overwrite-confirmation pattern as Telegram's /new,
        # since starting fresh here has no separate thread to fall back to.
        view = discord.ui.View(timeout=None)

        async def confirm(button_interaction: discord.Interaction):
            await button_interaction.response.edit_message(content="🆕 Starting a new session...", view=None)
            _create_session()
            await channel.send("✅ **Ready for new session!** Send a message to begin.")

        async def cancel(button_interaction: discord.Interaction):
            await button_interaction.response.edit_message(content="Cancelled.", view=None)

        btn_confirm = discord.ui.Button(label="✅ Yes, start new", style=discord.ButtonStyle.danger)
        btn_confirm.callback = confirm
        btn_cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        btn_cancel.callback = cancel
        view.add_item(btn_confirm)
        view.add_item(btn_cancel)

        await interaction.response.send_message(
            "⚠️ A session is already active in this DM. Starting a new one will lose its context. Continue?",
            view=view,
        )

    @cmd_new.autocomplete("cwd")
    async def cmd_new_cwd_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.cwd_autocomplete(interaction, current)

    @cmd_new.autocomplete("model")
    async def cmd_new_model_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.model_autocomplete(interaction, current)

    @app_commands.command(name="model", description="Change AI model")
    async def cmd_model(self, interaction: discord.Interaction, model: str):
        if not allowed(interaction.user.id):
            return await interaction.response.send_message("❌ Permission Denied", ephemeral=True)
        thread_id = str(interaction.channel_id)
        session = session_manager.get_session(thread_id)
        if not session:
            return await interaction.response.send_message("⚠️ Not an agy session thread.", ephemeral=True)

        try:
            exact = next((m for m in cached_models if m.lower() == model.lower()), None)
            partial = next((m for m in cached_models if model.lower() in m.lower()), None)
            final_model = exact or partial or model
            session_manager.update_session(str(interaction.channel_id), "model", final_model)

            bot_settings["default_model"] = final_model
            save_bot_settings(bot_settings)

            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"🤖 Model changed: **{final_model}**\n"
                    f"💾 Also set as the default for new `/new` sessions.",
                    color=EMBED_COLOR,
                )
            )
        except discord.Forbidden:
            logger.error(f"Missing permissions to start thread in channel {interaction.channel_id}")
            await interaction.response.send_message("❌ Missing permissions to start a thread here.", ephemeral=True)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"⚠️ An error occurred: {e}", ephemeral=True)

    @cmd_model.autocomplete("model")
    async def cmd_model_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.model_autocomplete(interaction, current)

    @app_commands.command(name="credit", description="Set whether to use AI Credits for this session (on/off)")
    @app_commands.describe(action="Turn AI Credits ON or OFF")
    @app_commands.choices(
        action=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]
    )
    async def cmd_credit(self, interaction: discord.Interaction, action: app_commands.Choice[str]):
        if not allowed(interaction.user.id):
            return await interaction.response.send_message("❌ Denied", ephemeral=True)

        settings_path = Path(os.getenv("HOME", "/root")) / ".gemini/antigravity-cli/settings.json"
        try:
            data = safe_load_json(settings_path, {}, logger=logger)

            use_credits = action.value == "on"
            data["useG1Credits"] = use_credits
            atomic_write_json(settings_path, data)

            status_text = "🟢 **ON** (Using AI Credits)" if use_credits else "🔴 **OFF** (Using default/free model)"
            await interaction.response.send_message(f"✅ AI Credit setting updated: {status_text}")
        except Exception as e:
            await interaction.response.send_message(f"⚠️ Failed to update settings: {e}", ephemeral=True)

    @app_commands.command(
        name="stop", description="Stop the currently generating response or task (Equivalent to ESC in CLI)"
    )
    async def cmd_stop(self, interaction: discord.Interaction):
        if not allowed(interaction.user.id):
            return await interaction.response.send_message("❌ Denied", ephemeral=True)
        thread_id = str(interaction.channel_id)

        session = session_manager.get_session(thread_id)
        if session:
            conv_id = session.get("conversation_id")
            future = session_manager.get_pending_approval_by_conv(conv_id)
            if future and not future.done():
                future.set_result("reject")

            prev_tts = session_manager.get_tts_task(thread_id)
            if prev_tts and not prev_tts.done():
                prev_tts.cancel()
                session_manager.remove_tts_task(thread_id)

            # Must clear conversation_id too, not just status - pending requires both unset.
            session_manager.set_session(thread_id, {**session, "status": "pending", "conversation_id": None})

        from core.agy_runner import stop_active_process

        if stop_active_process(thread_id):
            await interaction.response.send_message("🛑 Process stopped natively.", ephemeral=True)
        else:
            await interaction.response.send_message("🛑 Process stopped.", ephemeral=True)

    @app_commands.command(name="list", description="Active session list")
    async def cmd_sessions(self, interaction: discord.Interaction):
        if not allowed(interaction.user.id):
            return await interaction.response.send_message("❌ Denied", ephemeral=True)
        all_sessions = session_manager.get_all_sessions()
        if not all_sessions:
            return await interaction.response.send_message("No active sessions.", ephemeral=True)

        embed = discord.Embed(title="📋 Session List", color=EMBED_COLOR)
        from utils.utils import get_current_model

        for thread_id, sess in list(all_sessions.items())[-10:]:
            ch = self.bot.get_channel(int(thread_id))
            if isinstance(ch, discord.DMChannel):
                label = f"DM: {ch.recipient.display_name if ch.recipient else thread_id}"
            else:
                label = f"#{getattr(ch, 'name', f'ID:{thread_id}')}"
            embed.add_field(
                name=label,
                value=f"🤖 {MODEL_CHOICES.get(sess.get('model'), sess.get('model')) or get_current_model()}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
