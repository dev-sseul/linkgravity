"""Discord implementation of MessengerAdapter. All discord.py UI code
(buttons, modals, embeds) for the main messaging path lives here."""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import discord

from config import logger
from messengers.base import (
    IncomingAttachment,
    IncomingMessage,
    MessengerAdapter,
    PromptHandle,
    ScopeOption,
    ToolApprovalOutcome,
    VoiceCapable,
)


class _DiscordPromptHandle(PromptHandle):
    def __init__(self, embed: discord.Embed, view: discord.ui.View):
        self.embed = embed
        self.view = view
        self.message: discord.Message | None = None
        self.outcome: ToolApprovalOutcome | None = None

    async def send(self, conversation_ref: discord.abc.Messageable) -> discord.Message:
        self.message = await conversation_ref.send(embed=self.embed, view=self.view)
        return self.message

    async def finalize(self) -> None:
        for child in self.view.children:
            child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.embed, view=self.view)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning(f"Failed to finalize prompt message: {e}")


class DiscordAdapter(MessengerAdapter):
    platform_name = "discord"

    def __init__(self, bot: discord.Client):
        self.bot = bot

    # -- inbound -------------------------------------------------------------

    def to_incoming_message(self, raw_event: discord.Message) -> IncomingMessage | None:
        message = raw_event
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return None
        if message.author.bot:
            return None

        attachments = [
            IncomingAttachment(filename=att.filename, content_type=att.content_type, reader=att.read)
            for att in message.attachments
        ]

        async def add_reaction(emoji: str) -> None:
            await message.add_reaction(emoji)

        return IncomingMessage(
            author_id=message.author.id,
            content=message.content,
            conversation_id=str(message.channel.id),
            conversation_ref=message.channel,
            attachments=attachments,
            add_reaction=add_reaction,
        )

    # -- plain messaging ---------------------------------------------------

    async def send_message(self, conversation_ref: discord.abc.Messageable, text: str) -> discord.Message:
        return await conversation_ref.send(text)

    async def edit_message(self, message_ref: discord.Message, text: str) -> bool:
        try:
            await message_ref.edit(content=text, embed=None)
            return True
        except discord.NotFound:
            logger.warning("Discord message not found (likely deleted).")
            return False
        except discord.Forbidden:
            logger.error("Forbidden to edit Discord message (missing permissions).")
            return False
        except discord.HTTPException as e:
            logger.warning(f"HTTPException editing Discord message: {e}")
            return False

    async def send_files(self, conversation_ref: discord.abc.Messageable, file_paths: list[str]) -> None:
        if not file_paths:
            return
        await conversation_ref.send(files=[discord.File(p) for p in file_paths])

    def resolve_conversation(self, conversation_id: str) -> Any:
        try:
            return self.bot.get_channel(int(conversation_id))
        except (TypeError, ValueError):
            return None

    async def start_conversation(self, origin_ref: discord.Message, title: str) -> discord.Thread:
        return await origin_ref.create_thread(name=title[:100], auto_archive_duration=1440)

    async def rename_conversation(self, conversation_ref: discord.Thread, title: str) -> None:
        await conversation_ref.edit(name=title[:100])

    @asynccontextmanager
    async def typing(self, conversation_ref: discord.abc.Messageable):
        async with conversation_ref.typing():
            yield

    # -- interactive prompts ----------------------------------------------

    def create_tool_approval_prompt(
        self,
        decision_future: asyncio.Future,
        title: str,
        body: str,
        scope_options: list[ScopeOption],
    ) -> PromptHandle:
        embed = discord.Embed(title=title, description=body, color=discord.Color.orange())
        view = discord.ui.View(timeout=None)
        handle = _DiscordPromptHandle(embed, view)

        def make_callback(decision: str, scope: ScopeOption | None):
            async def callback(interaction: discord.Interaction):
                handle.outcome = ToolApprovalOutcome(decision=decision, scope=scope)
                if not decision_future.done():
                    decision_future.set_result(decision)

                for child in view.children:
                    child.disabled = True
                if decision == "allow" and scope:
                    embed.color = discord.Color.green()
                    embed.title = f"✅ Approved & Auto-Allowed ({scope.scope})"
                elif decision == "allow":
                    embed.color = discord.Color.green()
                    embed.title = "✅ Tool Execution Approved"
                else:
                    embed.color = discord.Color.red()
                    embed.title = "❌ Tool Execution Rejected"
                await interaction.response.edit_message(embed=embed, view=view)

            return callback

        btn_once = discord.ui.Button(label="✅ Approve once", style=discord.ButtonStyle.green)
        btn_once.callback = make_callback("allow", None)
        view.add_item(btn_once)

        for opt in scope_options:
            suffix = " tool" if opt.kind == "tools" else ""
            label = f"♾️ Allow [{opt.scope}]{suffix}"
            if len(label) > 80:
                label = f"♾️ Allow […{opt.scope[-68:]}]{suffix}"[:80]
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.gray)
            btn.callback = make_callback("allow", opt)
            view.add_item(btn)

        btn_reject = discord.ui.Button(label="❌ Reject", style=discord.ButtonStyle.red)
        btn_reject.callback = make_callback("reject", None)
        view.add_item(btn_reject)

        return handle

    def create_question_prompt(
        self,
        answer_future: asyncio.Future,
        question: str,
        options: list[str],
        multi_select: bool = False,
        allow_write_in: bool = True,
    ) -> PromptHandle:
        embed = discord.Embed(
            title="❓ Question from AI",
            description=f"**{question}**\n\nPlease select an answer below.",
            color=discord.Color.blue(),
        )
        view = discord.ui.View(timeout=None)
        handle = _DiscordPromptHandle(embed, view)

        async def _resolve(interaction: discord.Interaction, text: str, note: str):
            await interaction.response.send_message(f"✅ {note}: **{text}**")
            if not answer_future.done():
                answer_future.set_result(text)
            for child in view.children:
                child.disabled = True
            if interaction.message:
                try:
                    await interaction.message.edit(view=view)
                except discord.HTTPException:
                    pass

        if multi_select and options:
            select = discord.ui.Select(
                placeholder="Select multiple options...",
                min_values=1,
                max_values=min(len(options), 25),
                options=[discord.SelectOption(label=opt[:100]) for opt in options[:25]],
            )
            view.add_item(select)

            async def submit_callback(interaction: discord.Interaction):
                if not select.values:
                    return
                await _resolve(interaction, ", ".join(select.values), "Selected")

            submit_btn = discord.ui.Button(label="Submit", style=discord.ButtonStyle.primary)
            submit_btn.callback = submit_callback
            view.add_item(submit_btn)
        else:

            def make_option_callback(opt_text: str):
                async def callback(interaction: discord.Interaction):
                    await _resolve(interaction, opt_text, "Selected")

                return callback

            # 25 components per view; leave room for the write-in button.
            for opt in options[:24]:
                btn = discord.ui.Button(label=opt[:80], style=discord.ButtonStyle.primary)
                btn.callback = make_option_callback(opt)
                view.add_item(btn)

        if allow_write_in:

            class WriteInModal(discord.ui.Modal, title="Write in"):
                answer = discord.ui.TextInput(
                    label="Enter your response",
                    style=discord.TextStyle.paragraph,
                    placeholder="Type your response here...",
                    required=True,
                    max_length=2000,
                )

                async def on_submit(modal_self, interaction: discord.Interaction):
                    await _resolve(interaction, modal_self.answer.value, "Selected (Write in)")

            async def write_in_callback(interaction: discord.Interaction):
                await interaction.response.send_modal(WriteInModal())

            write_in_btn = discord.ui.Button(label="✍️ Write in", style=discord.ButtonStyle.secondary)
            write_in_btn.callback = write_in_callback
            view.add_item(write_in_btn)

        return handle


class DiscordVoiceAdapter(DiscordAdapter, VoiceCapable):
    """Discord adapter with voice wired in. Separate class so it's
    visible at the type level which code paths need voice."""

    def __init__(self, bot: discord.Client, voice_cog):
        super().__init__(bot)
        self.voice_cog = voice_cog

    async def join_voice(self, guild_ref: Any, channel_ref: Any) -> None:
        raise NotImplementedError("Wire to VoiceCog's /join when migrating voice_cog.py")

    async def leave_voice(self, guild_ref: Any) -> None:
        raise NotImplementedError("Wire to VoiceCog's /leave when migrating voice_cog.py")

    async def play_tts(self, guild_ref: Any, audio_bytes: bytes) -> None:
        await self.voice_cog._play_audio(str(guild_ref), audio_bytes)
