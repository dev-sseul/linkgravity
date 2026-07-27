"""Telegram bot entrypoint. Separate process from main.py (Discord) for
now - see handoff notes for the still-open question of whether Discord
and Telegram should eventually share one process."""

import sys
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import TELEGRAM_TOKEN, allowed, bot_settings, logger, session_manager
from handlers.message_router import handle_message
from messengers.registry import set_adapter
from messengers.telegram_adapter import TelegramAdapter
from utils.utils import get_default_cwd


def _start_session(chat_id: int, user_id: int) -> dict:
    session = {
        "status": "pending",
        "user_id": user_id,
        "cwd": get_default_cwd(),
        "model": bot_settings.get("default_model") or None,
        "conversation_id": None,
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
    await update.message.reply_text("✅ **Ready for new session!** Send a message to begin.")


async def on_message(update: Update, context) -> None:
    await handle_message(None, update)


async def on_error(update: object, context) -> None:
    logger.exception(f"Unhandled exception in Telegram update: {context.error}")
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ **An internal bot error has occurred.** Please contact the developer or check the server logs.",
            )
        except Exception:
            pass


def build_application() -> Application:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    adapter = TelegramAdapter(app.bot)
    app.bot_data["adapter"] = adapter
    set_adapter(adapter)

    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CallbackQueryHandler(adapter.handle_callback_query))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    if not TELEGRAM_TOKEN:
        logger.critical("Missing TELEGRAM_TOKEN - set telegram_token in lgy.json first.")
        sys.exit(1)

    app = build_application()
    logger.info("✅ Telegram bot starting (polling mode)...")
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
