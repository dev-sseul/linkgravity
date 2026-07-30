"""Telegram implementation of MessengerAdapter. 1 chat = 1 session (no
forum-topic support yet) - see handoff notes for the forum-mode follow-up."""

import asyncio
import html
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import logger
from messengers.base import (
    IncomingAttachment,
    IncomingMessage,
    MessengerAdapter,
    PromptHandle,
    ScopeOption,
    ToolApprovalOutcome,
)

_CODE_BLOCK_RE = re.compile(r"```(?:\w+\n)?(.*?)```|`([^`\n]+)`", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def markdown_to_telegram_html(text: str) -> str:
    """Converts the Discord-flavored markdown this codebase generates (bold, code fences) into Telegram's HTML parse mode, escaping everything else."""
    out = []
    pos = 0
    for m in _CODE_BLOCK_RE.finditer(text):
        out.append(_BOLD_RE.sub(r"<b>\1</b>", html.escape(text[pos : m.start()])))
        content = m.group(1) if m.group(1) is not None else m.group(2)
        tag = "pre" if m.group(1) is not None else "code"
        out.append(f"<{tag}>{html.escape(content)}</{tag}>")
        pos = m.end()
    out.append(_BOLD_RE.sub(r"<b>\1</b>", html.escape(text[pos:])))
    return "".join(out)


async def safe_query_edit(query, **kwargs) -> None:
    try:
        await query.edit_message_text(**kwargs)
    except TelegramError as e:
        if "message is not modified" in str(e).lower():
            return
        logger.error(f"Failed to edit Telegram message via callback query: {e}")
        raise


class _TelegramPromptHandle(PromptHandle):
    def __init__(self, bot, text: str, reply_markup, cleanup: Callable[[], None] | None = None):
        self.bot = bot
        self.text = text
        self.reply_markup = reply_markup
        self._cleanup = cleanup
        self.chat_id: int | None = None
        self.message_id: int | None = None
        self.outcome: ToolApprovalOutcome | None = None

    async def send(self, conversation_ref: int) -> Message:
        text = self.text if len(self.text) <= 4000 else self.text[:3997] + "..."
        try:
            msg = await self.bot.send_message(
                chat_id=conversation_ref, text=text, reply_markup=self.reply_markup, parse_mode="HTML"
            )
        except TelegramError as e:
            logger.error(f"Failed to send Telegram prompt message: {e}")
            raise
        self.chat_id = msg.chat_id
        self.message_id = msg.message_id
        return msg

    async def finalize(self) -> None:
        if self._cleanup:
            self._cleanup()
        if self.message_id is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id, message_id=self.message_id, text=self.text, reply_markup=None, parse_mode="HTML"
            )
        except TelegramError as e:
            logger.warning(f"Failed to finalize Telegram prompt message: {e}")


class TelegramAdapter(MessengerAdapter):
    platform_name = "telegram"
    supports_renaming = False

    def __init__(self, bot):
        self.bot = bot
        # callback_data -> async handler(query); prompts add/remove their own keys here.
        self._callbacks: dict[str, Callable[[Any], Awaitable[None]]] = {}

    def register_callback(self, key: str, handler: Callable[[Any], Awaitable[None]]) -> None:
        self._callbacks[key] = handler

    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        handler = self._callbacks.pop(query.data, None)
        if handler is None:
            await query.answer("This action has expired.", show_alert=True)
            return
        await handler(query)

    # -- inbound -------------------------------------------------------------

    def _make_reader(self, file_id: str) -> Callable[[], Awaitable[bytes]]:
        async def _reader() -> bytes:
            file = await self.bot.get_file(file_id)
            data = await file.download_as_bytearray()
            return bytes(data)

        return _reader

    def to_incoming_message(self, raw_event: Update) -> IncomingMessage | None:
        update = raw_event
        message = update.message
        if message is None or message.from_user is None or message.from_user.is_bot:
            return None

        attachments = []
        if message.photo:
            largest = message.photo[-1]
            attachments.append(
                IncomingAttachment(
                    filename="photo.jpg", content_type="image/jpeg", reader=self._make_reader(largest.file_id)
                )
            )
        if message.document:
            attachments.append(
                IncomingAttachment(
                    filename=message.document.file_name or "file",
                    content_type=message.document.mime_type,
                    reader=self._make_reader(message.document.file_id),
                )
            )
        if message.voice:
            attachments.append(
                IncomingAttachment(
                    filename="voice.ogg",
                    content_type=message.voice.mime_type or "audio/ogg",
                    reader=self._make_reader(message.voice.file_id),
                )
            )
        if message.audio:
            attachments.append(
                IncomingAttachment(
                    filename=message.audio.file_name or "audio",
                    content_type=message.audio.mime_type,
                    reader=self._make_reader(message.audio.file_id),
                )
            )

        async def add_reaction(emoji: str) -> None:
            try:
                await self.bot.set_message_reaction(
                    chat_id=message.chat_id, message_id=message.message_id, reaction=[ReactionTypeEmoji(emoji=emoji)]
                )
            except TelegramError as e:
                logger.warning(f"Failed to set Telegram reaction: {e}")

        return IncomingMessage(
            author_id=message.from_user.id,
            platform=self.platform_name,
            content=message.text or message.caption or "",
            conversation_id=str(message.chat_id),
            conversation_ref=message.chat_id,
            attachments=attachments,
            add_reaction=add_reaction,
        )

    # -- plain messaging ---------------------------------------------------

    async def send_message(self, conversation_ref: int, text: str) -> Message:
        try:
            return await self.bot.send_message(
                chat_id=conversation_ref, text=markdown_to_telegram_html(text), parse_mode="HTML"
            )
        except TelegramError as e:
            logger.error(f"Failed to send Telegram message: {e}")
            raise

    async def edit_message(self, message_ref: Message, text: str) -> bool:
        try:
            await self.bot.edit_message_text(chat_id=message_ref.chat_id, message_id=message_ref.message_id, text=text)
            return True
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                return True  # already showing this exact text - not a real failure, don't send a duplicate
            logger.warning(f"Failed to edit Telegram message: {e}")
            return False

    async def send_files(self, conversation_ref: int, file_paths: list[str]) -> None:
        for path in file_paths:
            with open(path, "rb") as f:
                await self.bot.send_document(chat_id=conversation_ref, document=f)

    def resolve_conversation(self, conversation_id: str) -> Any:
        try:
            return int(conversation_id)
        except (TypeError, ValueError):
            return None

    async def start_conversation(self, origin_ref: int, title: str) -> int:
        # Non-forum mode: the chat itself is the session, nothing to create.
        return origin_ref

    async def rename_conversation(self, conversation_ref: int, title: str) -> None:
        pass  # No per-session title surface outside forum-topic mode.

    @asynccontextmanager
    async def typing(self, conversation_ref: int):
        async def _keep_typing():
            while True:
                with suppress(TelegramError):
                    await self.bot.send_chat_action(chat_id=conversation_ref, action=ChatAction.TYPING)
                await asyncio.sleep(4)

        task = asyncio.create_task(_keep_typing())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    # -- interactive prompts ----------------------------------------------

    def create_tool_approval_prompt(
        self,
        decision_future: asyncio.Future,
        title: str,
        body: str,
        scope_options: list[ScopeOption],
    ) -> PromptHandle:
        prompt_id = uuid.uuid4().hex[:12]
        text = f"<b>{html.escape(title)}</b>\n\n{markdown_to_telegram_html(body)}"
        keys: list[str] = []
        keyboard: list[list[InlineKeyboardButton]] = []

        async def resolve(decision: str, scope: ScopeOption | None, query):
            handle.outcome = ToolApprovalOutcome(decision=decision, scope=scope)
            if not decision_future.done():
                decision_future.set_result(decision)
            if decision == "allow" and scope:
                new_text = f"✅ <b>Approved &amp; auto-allowed ({html.escape(scope.scope)})</b>"
            elif decision == "allow":
                new_text = "✅ <b>Approved</b>"
            else:
                new_text = "❌ <b>Rejected</b>"
            handle.text = new_text
            await query.answer()
            await safe_query_edit(query, text=new_text, parse_mode="HTML")

        allow_key = f"{prompt_id}:allow"
        self._callbacks[allow_key] = lambda query: resolve("allow", None, query)
        keys.append(allow_key)
        keyboard.append([InlineKeyboardButton("✅ Approve once", callback_data=allow_key)])

        for i, opt in enumerate(scope_options):
            suffix = " tool" if opt.kind == "tools" else ""
            label = f"♾️ Allow [{opt.scope}]{suffix}"
            if len(label) > 64:
                label = label[:61] + "…"
            key = f"{prompt_id}:scope:{i}"
            self._callbacks[key] = lambda query, opt=opt: resolve("allow", opt, query)
            keys.append(key)
            keyboard.append([InlineKeyboardButton(label, callback_data=key)])

        reject_key = f"{prompt_id}:reject"
        self._callbacks[reject_key] = lambda query: resolve("reject", None, query)
        keys.append(reject_key)
        keyboard.append([InlineKeyboardButton("❌ Reject", callback_data=reject_key)])

        handle = _TelegramPromptHandle(
            self.bot, text, InlineKeyboardMarkup(keyboard), cleanup=lambda: [self._callbacks.pop(k, None) for k in keys]
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
        text = f"❓ <b>Question from AI</b>\n\n<b>{html.escape(question)}</b>\n\nPlease choose an answer below."
        keys: list[str] = []

        async def resolve(chosen_text: str, note: str, query):
            if not answer_future.done():
                answer_future.set_result(chosen_text)
            new_text = f"✅ <b>{note}: {html.escape(chosen_text)}</b>"
            handle.text = new_text
            await query.answer()
            await safe_query_edit(query, text=new_text, parse_mode="HTML")

        async def write_in(query):
            await query.answer()
            await safe_query_edit(
                query,
                text=f"❓ <b>{html.escape(question)}</b>\n\n💬 Reply with your answer as a message.",
                parse_mode="HTML",
                reply_markup=ForceReply(selective=True),
            )

        if allow_write_in:
            write_in_key = f"{prompt_id}:write_in"
            self._callbacks[write_in_key] = write_in
            keys.append(write_in_key)

        if multi_select and options:
            selected: set[int] = set()
            shown = options[:20]

            def render_keyboard() -> InlineKeyboardMarkup:
                rows = [
                    [
                        InlineKeyboardButton(
                            ("☑️ " if i in selected else "⬜ ") + opt[:60], callback_data=f"{prompt_id}:toggle:{i}"
                        )
                    ]
                    for i, opt in enumerate(shown)
                ]
                rows.append([InlineKeyboardButton("Submit", callback_data=f"{prompt_id}:submit")])
                if allow_write_in:
                    rows.append([InlineKeyboardButton("✍️ Write in", callback_data=f"{prompt_id}:write_in")])
                return InlineKeyboardMarkup(rows)

            async def toggle(i: int, query):
                selected.symmetric_difference_update({i})
                await query.answer()
                await query.edit_message_reply_markup(reply_markup=render_keyboard())

            async def submit(query):
                if not selected:
                    await query.answer("Select at least one option first.", show_alert=True)
                    return
                chosen = ", ".join(shown[i] for i in sorted(selected))
                await resolve(chosen, "Selected", query)

            for i in range(len(shown)):
                key = f"{prompt_id}:toggle:{i}"
                self._callbacks[key] = lambda query, i=i: toggle(i, query)
                keys.append(key)
            self._callbacks[f"{prompt_id}:submit"] = submit
            keys.append(f"{prompt_id}:submit")
            keyboard = render_keyboard().inline_keyboard
        else:
            keyboard = []
            for i, opt in enumerate(options[:20]):
                key = f"{prompt_id}:opt:{i}"
                self._callbacks[key] = lambda query, opt=opt: resolve(opt, "Selected", query)
                keys.append(key)
                keyboard.append([InlineKeyboardButton(opt[:64], callback_data=key)])
            if allow_write_in:
                keyboard.append([InlineKeyboardButton("✍️ Write in", callback_data=f"{prompt_id}:write_in")])

        handle = _TelegramPromptHandle(
            self.bot, text, InlineKeyboardMarkup(keyboard), cleanup=lambda: [self._callbacks.pop(k, None) for k in keys]
        )
        return handle
