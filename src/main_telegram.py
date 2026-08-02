"""Telegram bot setup. Runs in the same process as Discord (see main.py),
which starts both concurrently when both platforms are enabled."""

import asyncio
import uuid
from datetime import datetime
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import TELEGRAM_TOKEN, allowed, bot_settings, logger, save_bot_settings, session_manager
from core.atomic_io import atomic_write_json, safe_load_json
from handlers.message_router import handle_message
from messengers.registry import register_adapter
from messengers.telegram_adapter import TelegramAdapter, markdown_to_telegram_html, safe_query_edit
from utils.utils import get_default_cwd


def _start_session(chat_id: int, user_id: int) -> dict:
    session = {
        "status": "pending",
        "platform": "telegram",
        "user_id": user_id,
        "cwd": get_default_cwd(),
        "model": bot_settings.get("default_model") or None,
        "conversation_id": None,
        "created_at": datetime.now().isoformat(),
    }
    session_manager.set_session(str(chat_id), session)
    return session


async def cmd_new(update: Update, context) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    adapter: TelegramAdapter = context.bot_data["adapter"]

    if not allowed(user.id, "telegram"):
        await update.message.reply_text("❌ Permission denied.")
        return

    if session_manager.get_session(str(chat_id)):
        prompt_id = uuid.uuid4().hex[:12]
        confirm_key, cancel_key = f"{prompt_id}:confirm", f"{prompt_id}:cancel"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Yes, start new", callback_data=confirm_key),
                    InlineKeyboardButton("Cancel", callback_data=cancel_key),
                ]
            ]
        )
        await update.message.reply_text(
            "⚠️ A session is already active in this chat. Starting a new one will lose its context. Continue?",
            reply_markup=keyboard,
        )

        async def confirm(query):
            await query.answer()
            await query.edit_message_text("🆕 Starting a new session...", reply_markup=None)
            _start_session(chat_id, user.id)
            await adapter.send_message(chat_id, "✅ **Ready for new session!** Send a message to begin.")

        async def cancel(query):
            await query.answer()
            await query.edit_message_text("Cancelled.", reply_markup=None)

        adapter.register_callback(confirm_key, confirm)
        adapter.register_callback(cancel_key, cancel)
        return

    _start_session(chat_id, user.id)
    await update.message.reply_text(
        markdown_to_telegram_html("✅ **Ready for new session!** Send a message to begin."), parse_mode="HTML"
    )


async def cmd_model(update: Update, context) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    adapter: TelegramAdapter = context.bot_data["adapter"]

    if not allowed(user.id, "telegram"):
        await update.message.reply_text("❌ Permission Denied")
        return

    session = session_manager.get_session(str(chat_id))
    if not session:
        await update.message.reply_text("⚠️ No active session here. Start one with /new first.")
        return

    from cogs.general_cog import load_cached_models

    cached_models = load_cached_models()
    current_model = session.get("model") or bot_settings.get("default_model")

    def _apply_model(final_model: str) -> str:
        session_manager.update_session(str(chat_id), "model", final_model)
        bot_settings["default_model"] = final_model
        save_bot_settings(bot_settings)
        return final_model

    if not context.args:
        prompt_id = uuid.uuid4().hex[:12]
        keyboard = []
        for m in cached_models:
            key = f"{prompt_id}:{m}"
            label = ("✅ " if m == current_model else "") + m

            async def pick(query, m=m):
                final_model = _apply_model(m)
                await query.answer()
                await safe_query_edit(
                    query,
                    text=markdown_to_telegram_html(f"🤖 Model changed: **{final_model}**"),
                    parse_mode="HTML",
                )

            adapter.register_callback(key, pick)
            keyboard.append([InlineKeyboardButton(label, callback_data=key)])

        await update.message.reply_text(
            "Pick a model (or send /model <name> to type one):", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    requested = " ".join(context.args)
    exact = next((m for m in cached_models if m.lower() == requested.lower()), None)
    partial = next((m for m in cached_models if requested.lower() in m.lower()), None)
    final_model = _apply_model(exact or partial or requested)

    await adapter.send_message(
        chat_id, f"🤖 Model changed: **{final_model}**\n💾 Also set as the default for new sessions."
    )


async def cmd_credit(update: Update, context) -> None:
    user = update.effective_user
    adapter: TelegramAdapter = context.bot_data["adapter"]

    if not allowed(user.id, "telegram"):
        await update.message.reply_text("❌ Denied")
        return

    settings_path = Path.home() / ".gemini/antigravity-cli/settings.json"
    current = bool(safe_load_json(settings_path, {}, logger=logger).get("useG1Credits", False))

    async def set_credit(use_credits: bool, query) -> None:
        try:
            data = safe_load_json(settings_path, {}, logger=logger)
            data["useG1Credits"] = use_credits
            atomic_write_json(settings_path, data)
        except Exception as e:
            await query.answer()
            await safe_query_edit(query, text=f"⚠️ Failed to update settings: {e}")
            return

        status_text = "🟢 **ON** (Using AI Credits)" if use_credits else "🔴 **OFF** (Using default/free model)"
        await query.answer()
        await safe_query_edit(
            query,
            text=markdown_to_telegram_html(f"✅ AI Credit setting updated: {status_text}"),
            parse_mode="HTML",
        )

    prompt_id = uuid.uuid4().hex[:12]
    on_key, off_key = f"{prompt_id}:on", f"{prompt_id}:off"
    adapter.register_callback(on_key, lambda query: set_credit(True, query))
    adapter.register_callback(off_key, lambda query: set_credit(False, query))

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(("✅ " if current else "") + "🟢 ON", callback_data=on_key),
                InlineKeyboardButton(("✅ " if not current else "") + "🔴 OFF", callback_data=off_key),
            ]
        ]
    )
    await update.message.reply_text("AI Credits:", reply_markup=keyboard)


async def on_message(update: Update, context) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    if user and allowed(user.id, "telegram") and not session_manager.get_session(str(chat_id)):
        _start_session(chat_id, user.id)
    await handle_message(None, update, context.bot_data["adapter"])


async def on_error(update: object, context) -> None:
    logger.exception(f"Unhandled exception in Telegram update: {context.error}")
    if isinstance(update, Update) and update.effective_chat:
        try:
            adapter = context.bot_data["adapter"]
            await adapter.send_message(
                update.effective_chat.id,
                "⚠️ **An internal bot error has occurred.** Please contact the developer or check the server logs.",
            )
        except Exception:
            pass


async def on_ready(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("new", "Start a new session (or /start)"),
            BotCommand("model", "Change the AI model for this session"),
            BotCommand("credit", "Turn AI Credits on/off"),
        ]
    )
    logger.info(f"✅ Bot is fully online and ready! Logged in as @{app.bot.username}")


def build_application() -> Application:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_ready).concurrent_updates(True).build()
    adapter = TelegramAdapter(app.bot)
    app.bot_data["adapter"] = adapter
    register_adapter("telegram", adapter)

    app.add_handler(CommandHandler(["new", "start"], cmd_new))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("credit", cmd_credit))
    app.add_handler(CallbackQueryHandler(adapter.handle_callback_query))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app


async def run_telegram(stop_event: asyncio.Event) -> None:
    """Runs the Telegram bot manually (not via Application.run_polling(),
    which blocks and manages its own event loop) so it can run alongside
    Discord in the same process. See PTB docs on combining Application
    with other asyncio frameworks."""
    if not TELEGRAM_TOKEN:
        logger.critical("Missing TELEGRAM_TOKEN - set telegram_token in lgy.json first.")
        return

    app = build_application()
    logger.info("✅ Telegram bot starting (polling mode)...")
    await app.initialize()
    if app.post_init:  # not auto-called outside run_polling()/run_webhook()
        await app.post_init(app)
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    from core import platform_health

    platform_health.set_status("telegram", "running")
    try:
        await stop_event.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if app.post_shutdown:  # same gotcha as post_init: not auto-called here
            await app.post_shutdown(app)
