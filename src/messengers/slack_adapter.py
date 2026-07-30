"""Slack implementation of MessengerAdapter. Slack has real threads, so this
matches Discord's model (channel message starts a thread) rather than
Telegram's. conversation_id is "channel:thread_ts"."""

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from slack_bolt.app.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from config import logger, session_manager
from messengers.base import (
    IncomingAttachment,
    IncomingMessage,
    MessengerAdapter,
    PromptHandle,
    ScopeOption,
    ToolApprovalOutcome,
)

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def markdown_to_slack_mrkdwn(text: str) -> str:
    """Converts the Discord-flavored markdown this codebase generates
    (**bold**) into Slack's mrkdwn (*bold*). Code fences are already
    compatible between the two, so left as-is."""
    return _BOLD_RE.sub(r"*\1*", text)


def encode_conversation_id(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def decode_conversation_id(conversation_id: str) -> tuple[str, str] | None:
    if ":" not in conversation_id:
        return None
    channel, _, thread_ts = conversation_id.partition(":")
    return channel, thread_ts


def latest_channel_session(channel: str) -> tuple[str, dict] | None:
    """Most recently created Slack session in a channel - used as a fallback for un-threaded
    messages (users rarely bother clicking "Reply in thread") and for /model, /credit, which
    can't target a specific thread since Slack slash commands can't be invoked inside one."""
    candidates = [
        (cid, s)
        for cid, s in session_manager.get_all_sessions().items()
        if s.get("platform") == "slack" and cid.startswith(f"{channel}:")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda kv: kv[1].get("created_at", ""))


class SlackConversationRef:
    """conversation_ref for Slack - a channel + the thread_ts all replies go under."""

    __slots__ = ("channel", "thread_ts")

    def __init__(self, channel: str, thread_ts: str):
        self.channel = channel
        self.thread_ts = thread_ts

    def __repr__(self) -> str:
        return f"SlackConversationRef({self.channel}, {self.thread_ts})"

    @property
    def api_thread_ts(self) -> str | None:
        """None for DMs (thread_ts is a "channel:channel" sentinel there, not a real ts)."""
        return None if self.thread_ts == self.channel else self.thread_ts


class SlackMessageRef:
    """message_ref for edit_message - a specific message within a channel."""

    __slots__ = ("channel", "ts")

    def __init__(self, channel: str, ts: str):
        self.channel = channel
        self.ts = ts


class _SlackPromptHandle(PromptHandle):
    def __init__(
        self, client: AsyncWebClient, text: str, blocks: list[dict], cleanup: Callable[[], None] | None = None
    ):
        self.client = client
        self.text = text
        self.blocks = blocks
        self._cleanup = cleanup
        self.channel: str | None = None
        self.ts: str | None = None
        self.outcome: ToolApprovalOutcome | None = None

    async def send(self, conversation_ref: SlackConversationRef) -> dict:
        try:
            resp = await self.client.chat_postMessage(
                channel=conversation_ref.channel,
                thread_ts=conversation_ref.api_thread_ts,
                text=self.text,
                blocks=self.blocks,
            )
        except SlackApiError as e:
            logger.error(f"Failed to send Slack prompt message: {e}")
            raise
        self.channel = resp["channel"]
        self.ts = resp["ts"]
        return resp

    async def finalize(self) -> None:
        if self._cleanup:
            self._cleanup()
        if self.ts is None:
            return
        try:
            await self.client.chat_update(channel=self.channel, ts=self.ts, text=self.text, blocks=self.blocks)
        except SlackApiError as e:
            logger.warning(f"Failed to finalize Slack prompt message: {e}")


class SlackAdapter(MessengerAdapter):
    platform_name = "slack"
    supports_renaming = True  # No thread "title" field in Slack, but we edit the parent message text instead.

    def __init__(self, app: AsyncApp):
        self.app = app
        self.client: AsyncWebClient = app.client
        self._bot_user_id: str | None = None
        # action_id -> async handler(body, client) ; prompts add/remove their own keys here.
        self._callbacks: dict[str, Callable[[dict, AsyncWebClient], Awaitable[None]]] = {}
        # view callback_id -> async handler(body, client) for modal (write-in) submissions.
        self._view_callbacks: dict[str, Callable[[dict, AsyncWebClient], Awaitable[None]]] = {}

    async def resolve_bot_user_id(self) -> str:
        if self._bot_user_id is None:
            auth = await self.client.auth_test()
            self._bot_user_id = auth["user_id"]
        return self._bot_user_id

    def register_callback(self, action_id: str, handler: Callable[[dict, AsyncWebClient], Awaitable[None]]) -> None:
        self._callbacks[action_id] = handler

    def register_view_callback(
        self, callback_id: str, handler: Callable[[dict, AsyncWebClient], Awaitable[None]]
    ) -> None:
        self._view_callbacks[callback_id] = handler

    async def handle_block_action(self, body: dict) -> None:
        actions = body.get("actions") or []
        if not actions:
            return
        action_id = actions[0].get("action_id")
        handler = self._callbacks.pop(action_id, None)
        if handler is None:
            return  # expired/unknown action - nothing to do, Bolt already acked
        await handler(body, self.client)

    async def handle_view_submission(self, body: dict) -> None:
        callback_id = (body.get("view") or {}).get("callback_id")
        handler = self._view_callbacks.pop(callback_id, None)
        if handler is None:
            return
        await handler(body, self.client)

    def _make_reader(self, url: str) -> Callable[[], Awaitable[bytes]]:
        async def _download() -> bytes:
            # Private file URLs require bot-token auth (unlike public asset URLs).
            import aiohttp

            headers = {"Authorization": f"Bearer {self.client.token}"}
            async with aiohttp.ClientSession() as session, session.get(url, headers=headers) as resp:
                return await resp.read()

        return _download

    def to_incoming_message(self, raw_event: dict) -> IncomingMessage | None:
        event = raw_event
        if event.get("bot_id") or event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
            return None
        if event.get("user") == self._bot_user_id:
            return None
        channel = event.get("channel")
        user = event.get("user")
        text = event.get("text", "")
        if channel is None or user is None:
            return None

        # DMs use a flat model like Telegram (no threading expected).
        if event.get("channel_type") == "im":
            thread_ts = channel
        elif event.get("thread_ts"):
            thread_ts = event["thread_ts"]  # explicit "Reply in thread" - honor it exactly
        else:
            # Most users don't bother threading replies, so a plain channel message falls back
            # to that channel's most recent session instead of starting a disconnected new one.
            found = latest_channel_session(channel)
            thread_ts = decode_conversation_id(found[0])[1] if found else event["ts"]
        conversation_id = encode_conversation_id(channel, thread_ts)
        ref = SlackConversationRef(channel, thread_ts)

        attachments = []
        for f in event.get("files") or []:
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            attachments.append(
                IncomingAttachment(
                    filename=f.get("name") or "file", content_type=f.get("mimetype"), reader=self._make_reader(url)
                )
            )

        async def add_reaction(emoji: str) -> None:
            try:
                await self.client.reactions_add(channel=channel, timestamp=event["ts"], name=emoji.strip(":"))
            except SlackApiError as e:
                logger.warning(f"Failed to set Slack reaction: {e}")

        return IncomingMessage(
            author_id=user,
            platform=self.platform_name,
            content=text,
            conversation_id=conversation_id,
            conversation_ref=ref,
            attachments=attachments,
            add_reaction=add_reaction,
        )

    async def send_message(self, conversation_ref: SlackConversationRef, text: str) -> dict:
        try:
            return await self.client.chat_postMessage(
                channel=conversation_ref.channel,
                thread_ts=conversation_ref.api_thread_ts,
                text=markdown_to_slack_mrkdwn(text),
            )
        except SlackApiError as e:
            logger.error(f"Failed to send Slack message: {e}")
            raise

    async def edit_message(self, message_ref: dict, text: str) -> bool:
        try:
            await self.client.chat_update(
                channel=message_ref["channel"], ts=message_ref["ts"], text=markdown_to_slack_mrkdwn(text)
            )
            return True
        except SlackApiError as e:
            if "message_not_found" in str(e):
                return False
            logger.warning(f"Failed to edit Slack message: {e}")
            return False

    async def send_files(self, conversation_ref: SlackConversationRef, file_paths: list[str]) -> None:
        for path in file_paths:
            try:
                await self.client.files_upload_v2(
                    channel=conversation_ref.channel, thread_ts=conversation_ref.api_thread_ts, file=path
                )
            except SlackApiError as e:
                logger.error(f"Failed to send Slack file {path}: {e}")
                raise

    def resolve_conversation(self, conversation_id: str) -> Any:
        decoded = decode_conversation_id(conversation_id)
        if decoded is None:
            return None
        channel, thread_ts = decoded
        return SlackConversationRef(channel, thread_ts)

    async def start_conversation(self, origin_ref: dict, title: str) -> SlackConversationRef:
        # No explicit "create thread" call in Slack - the origin message's own ts becomes thread_ts.
        return SlackConversationRef(origin_ref["channel"], origin_ref["ts"])

    async def rename_conversation(self, conversation_ref: SlackConversationRef, title: str) -> None:
        if conversation_ref.api_thread_ts is None:
            return  # DM has no announcement message to update
        try:
            await self.client.chat_update(
                channel=conversation_ref.channel,
                ts=conversation_ref.thread_ts,
                text=f"🧵 *{markdown_to_slack_mrkdwn(title)}*",
            )
        except SlackApiError as e:
            logger.warning(f"Failed to update Slack thread summary: {e}")

    def create_tool_approval_prompt(
        self,
        decision_future: asyncio.Future,
        title: str,
        body: str,
        scope_options: list[ScopeOption],
    ) -> PromptHandle:
        prompt_id = uuid.uuid4().hex[:12]
        text = f"*{title}*\n\n{markdown_to_slack_mrkdwn(body)}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2990]}},
        ]
        keys: list[str] = []
        elements = []

        async def resolve(decision: str, scope: ScopeOption | None, resp_body: dict, client: AsyncWebClient):
            handle.outcome = ToolApprovalOutcome(decision=decision, scope=scope)
            if not decision_future.done():
                decision_future.set_result(decision)
            if decision == "allow" and scope:
                new_text = f"✅ *Approved & auto-allowed ({scope.scope})*"
            elif decision == "allow":
                new_text = "✅ *Approved*"
            else:
                new_text = "❌ *Rejected*"
            handle.text = new_text
            handle.blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": new_text}}]
            await handle.finalize()

        allow_key = f"{prompt_id}:allow"
        self._callbacks[allow_key] = lambda b, c: resolve("allow", None, b, c)
        keys.append(allow_key)
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Approve once"},
                "action_id": allow_key,
                "style": "primary",
            }
        )

        for i, opt in enumerate(scope_options):
            suffix = " tool" if opt.kind == "tools" else ""
            label = f"♾️ Allow [{opt.scope}]{suffix}"
            if len(label) > 75:
                label = label[:72] + "…"
            key = f"{prompt_id}:scope:{i}"
            self._callbacks[key] = lambda b, c, opt=opt: resolve("allow", opt, b, c)
            keys.append(key)
            elements.append({"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": key})

        reject_key = f"{prompt_id}:reject"
        self._callbacks[reject_key] = lambda b, c: resolve("reject", None, b, c)
        keys.append(reject_key)
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "❌ Reject"},
                "action_id": reject_key,
                "style": "danger",
            }
        )

        # 25 is Slack's hard per-block limit on action elements.
        for chunk_start in range(0, len(elements), 25):
            blocks.append({"type": "actions", "elements": elements[chunk_start : chunk_start + 25]})

        handle = _SlackPromptHandle(
            self.client, text, blocks, cleanup=lambda: [self._callbacks.pop(k, None) for k in keys]
        )
        return handle

    def create_question_prompt(
        self,
        answer_future: asyncio.Future,
        question: str,
        options: list[str],
        multi_select: bool = False,
        allow_write_in: bool = True,
    ) -> PromptHandle:
        prompt_id = uuid.uuid4().hex[:12]
        text = f"❓ *Question from AI*\n\n*{question}*\n\nPlease choose an answer below."
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2990]}}]
        keys: list[str] = []

        async def resolve(chosen_text: str, note: str, body: dict, client: AsyncWebClient):
            if not answer_future.done():
                answer_future.set_result(chosen_text)
            new_text = f"✅ *{note}: {chosen_text}*"
            handle.text = new_text
            handle.blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": new_text}}]
            await handle.finalize()

        async def open_write_in_modal(body: dict, client: AsyncWebClient):
            view_callback_id = f"{prompt_id}:write_in_view"

            async def on_submit(submit_body: dict, submit_client: AsyncWebClient):
                value = submit_body["view"]["state"]["values"]["answer_block"]["answer_input"]["value"]
                await resolve(value, "Selected (Write in)", submit_body, submit_client)

            self.register_view_callback(view_callback_id, on_submit)
            await client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "callback_id": view_callback_id,
                    "title": {"type": "plain_text", "text": "Write in"},
                    "submit": {"type": "plain_text", "text": "Submit"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "answer_block",
                            "label": {"type": "plain_text", "text": "Enter your response"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "answer_input",
                                "multiline": True,
                                "max_length": 2000,
                            },
                        }
                    ],
                },
            )

        if allow_write_in:
            write_in_key = f"{prompt_id}:write_in"
            self._callbacks[write_in_key] = open_write_in_modal
            keys.append(write_in_key)

        if multi_select and options:
            selected: set[int] = set()
            shown = options[:20]
            toggle_prefix = f"{prompt_id}:toggle:"
            submit_key = f"{prompt_id}:submit"

            def render_elements() -> list[dict]:
                elements = [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ("☑️ " if i in selected else "⬜ ") + opt[:60]},
                        "action_id": f"{toggle_prefix}{i}",
                    }
                    for i, opt in enumerate(shown)
                ]
                elements.append(
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Submit"},
                        "action_id": submit_key,
                        "style": "primary",
                    }
                )
                if allow_write_in:
                    elements.append(
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✍️ Write in"},
                            "action_id": write_in_key,
                        }
                    )
                return elements

            async def toggle(i: int, body: dict, client: AsyncWebClient):
                selected.symmetric_difference_update({i})
                handle.blocks = [blocks[0], {"type": "actions", "elements": render_elements()}]
                self._callbacks[f"{toggle_prefix}{i}"] = lambda b, c, i=i: toggle(
                    i, b, c
                )  # re-register for next toggle
                await self.client.chat_update(
                    channel=handle.channel, ts=handle.ts, text=handle.text, blocks=handle.blocks
                )

            async def submit(body: dict, client: AsyncWebClient):
                if not selected:
                    return
                chosen = ", ".join(shown[i] for i in sorted(selected))
                await resolve(chosen, "Selected", body, client)

            for i in range(len(shown)):
                key = f"{toggle_prefix}{i}"
                self._callbacks[key] = lambda b, c, i=i: toggle(i, b, c)
                keys.append(key)
            self._callbacks[submit_key] = submit
            keys.append(submit_key)
            blocks.append({"type": "actions", "elements": render_elements()})
        else:
            elements = []
            for i, opt in enumerate(options[:23]):
                key = f"{prompt_id}:opt:{i}"
                self._callbacks[key] = lambda b, c, opt=opt: resolve(opt, "Selected", b, c)
                keys.append(key)
                elements.append({"type": "button", "text": {"type": "plain_text", "text": opt[:75]}, "action_id": key})
            if allow_write_in:
                elements.append(
                    {"type": "button", "text": {"type": "plain_text", "text": "✍️ Write in"}, "action_id": write_in_key}
                )
            for chunk_start in range(0, len(elements), 25):
                blocks.append({"type": "actions", "elements": elements[chunk_start : chunk_start + 25]})

        handle = _SlackPromptHandle(
            self.client, text, blocks, cleanup=lambda: [self._callbacks.pop(k, None) for k in keys]
        )
        return handle
