"""Slack bot setup. Runs in the same process as Discord/Telegram (see
main.py), which starts all enabled platforms concurrently."""

import asyncio
import re
import uuid
from datetime import datetime

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from config import SLACK_APP_TOKEN, SLACK_BOT_TOKEN, allowed, bot_settings, logger, session_manager
from core import platform_health
from handlers.message_router import handle_message
from messengers.registry import register_adapter
from messengers.slack_adapter import SlackAdapter, encode_conversation_id, latest_channel_session
from utils.utils import get_default_cwd


def _start_session(conversation_id: str, user_id: str) -> dict:
    session = {
        "status": "pending",
        "platform": "slack",
        "user_id": user_id,
        "cwd": get_default_cwd(),
        "model": bot_settings.get("default_model") or None,
        "conversation_id": None,
        "created_at": datetime.now().isoformat(),
    }
    session_manager.set_session(conversation_id, session)
    return session


async def cmd_new(ack, body, respond, context) -> None:
    await ack()
    user_id = body["user_id"]
    channel = body["channel_id"]

    if not allowed(user_id, "slack"):
        await respond("❌ Permission denied.")
        return

    if body.get("channel_name") == "directmessage":
        # DMs are 1:1 like Telegram - no threading, so the channel itself is the session key.
        conversation_id = encode_conversation_id(channel, channel)
        _start_session(conversation_id, user_id)
        await respond("✅ *Ready for new session!* Send a message to begin.")
        return

    client = context["client"]
    # A slash command has no message ts to thread under, and (unlike Discord) Slack has no
    # explicit "create thread" call - so post the announcement first and thread off its ts.
    try:
        resp = await client.chat_postMessage(
            channel=channel, text="✅ *New session started!* Reply in this thread to begin."
        )
    except SlackApiError as e:
        if e.response.get("error") == "not_in_channel":
            await respond("❌ I'm not in this channel yet - run `/invite @<this bot>` here first, then try /new again.")
        else:
            await respond(f"❌ Failed to start a session: {e.response.get('error')}")
        return
    conversation_id = encode_conversation_id(channel, resp["ts"])
    _start_session(conversation_id, user_id)


async def cmd_model(ack, body, respond, context) -> None:
    await ack()
    adapter: SlackAdapter = context["adapter"]
    user_id = body["user_id"]
    channel = body["channel_id"]

    if not allowed(user_id, "slack"):
        await respond("❌ Permission Denied")
        return

    found = latest_channel_session(channel)
    if not found:
        await respond("⚠️ No active session here. Start one with /new first.")
        return
    conversation_id, session = found

    from cogs.general_cog import load_cached_models

    cached_models = load_cached_models()
    current_model = session.get("model") or bot_settings.get("default_model")

    def _apply_model(final_model: str) -> str:
        session_manager.update_session(conversation_id, "model", final_model)
        bot_settings["default_model"] = final_model
        from config import save_bot_settings

        save_bot_settings(bot_settings)
        return final_model

    requested = (body.get("text") or "").strip()
    if not requested:
        prompt_id = uuid.uuid4().hex[:12]
        elements = []
        for m in cached_models:
            key = f"{prompt_id}:{m}"
            label = ("✅ " if m == current_model else "") + m

            async def pick(action_body, client, m=m):
                final_model = _apply_model(m)
                await client.chat_postMessage(channel=channel, text=f"🤖 Model changed: *{final_model}*")

            adapter.register_callback(key, pick)
            elements.append({"type": "button", "text": {"type": "plain_text", "text": label[:75]}, "action_id": key})

        await respond(
            {
                "text": "Pick a model (or send /model <name> to type one):",
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "Pick a model (or send `/model <name>` to type one):"},
                    },
                    {"type": "actions", "elements": elements[:25]},
                ],
            }
        )
        return

    exact = next((m for m in cached_models if m.lower() == requested.lower()), None)
    partial = next((m for m in cached_models if requested.lower() in m.lower()), None)
    final_model = _apply_model(exact or partial or requested)
    await respond(f"🤖 Model changed: *{final_model}*\n💾 Also set as the default for new sessions.")


async def cmd_credit(ack, body, respond, context) -> None:
    await ack()
    adapter: SlackAdapter = context["adapter"]
    user_id = body["user_id"]
    channel = body["channel_id"]

    if not allowed(user_id, "slack"):
        await respond("❌ Denied")
        return

    from pathlib import Path

    from core.atomic_io import atomic_write_json, safe_load_json

    settings_path = Path.home() / ".gemini/antigravity-cli/settings.json"
    current = bool(safe_load_json(settings_path, {}, logger=logger).get("useG1Credits", False))

    async def set_credit(use_credits: bool, action_body, client) -> None:
        try:
            data = safe_load_json(settings_path, {}, logger=logger)
            data["useG1Credits"] = use_credits
            atomic_write_json(settings_path, data)
        except Exception as e:
            await client.chat_postMessage(channel=channel, text=f"⚠️ Failed to update settings: {e}")
            return
        status_text = "🟢 *ON* (Using AI Credits)" if use_credits else "🔴 *OFF* (Using default/free model)"
        await client.chat_postMessage(channel=channel, text=f"✅ AI Credit setting updated: {status_text}")

    prompt_id = uuid.uuid4().hex[:12]
    on_key, off_key = f"{prompt_id}:on", f"{prompt_id}:off"
    adapter.register_callback(on_key, lambda b, c: set_credit(True, b, c))
    adapter.register_callback(off_key, lambda b, c: set_credit(False, b, c))

    await respond(
        {
            "text": "AI Credits:",
            "blocks": [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": ("✅ " if current else "") + "🟢 ON"},
                            "action_id": on_key,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": ("✅ " if not current else "") + "🔴 OFF"},
                            "action_id": off_key,
                        },
                    ],
                }
            ],
        }
    )


async def on_message(event, context) -> None:
    await handle_message(None, event, context["adapter"])


async def on_action(ack, body, context) -> None:
    await ack()
    await context["adapter"].handle_block_action(body)


async def on_view_submission(ack, body, context) -> None:
    await ack()
    await context["adapter"].handle_view_submission(body)


def build_app() -> tuple[AsyncApp, SlackAdapter]:
    app = AsyncApp(token=SLACK_BOT_TOKEN)
    adapter = SlackAdapter(app)

    @app.middleware
    async def inject_adapter(context, next):
        context["adapter"] = adapter
        await next()

    app.command("/new")(cmd_new)
    app.command("/model")(cmd_model)
    app.command("/credit")(cmd_credit)
    app.event("message")(on_message)
    app.action(re.compile(".*"))(on_action)
    app.view(re.compile(".*"))(on_view_submission)

    register_adapter("slack", adapter)
    return app, adapter


async def run_slack(stop_event: asyncio.Event) -> None:
    """Uses connect_async()/close_async() directly - start_async() sleeps forever internally and never returns."""
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        logger.critical(
            "Missing SLACK_BOT_TOKEN/SLACK_APP_TOKEN - set slack_bot_token/slack_app_token in lgy.json first."
        )
        return
    if not SLACK_BOT_TOKEN.startswith("xoxb-"):
        logger.critical(
            "slack_bot_token doesn't start with xoxb- - looks like the User OAuth Token was used "
            "instead of the Bot User OAuth Token (OAuth & Permissions page has both)."
        )
        return

    app, adapter = build_app()
    try:
        await adapter.resolve_bot_user_id()
    except SlackApiError as e:
        logger.critical(f"Slack auth_test failed - check slack_bot_token: {e}")
        return

    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    logger.info("✅ Slack bot starting (Socket Mode)...")
    await handler.connect_async()
    platform_health.set_status("slack", "running")
    try:
        await stop_event.wait()
    finally:
        await handler.close_async()
