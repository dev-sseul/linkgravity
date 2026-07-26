import discord

from config import allowed
from handlers.thread_reply import handle_thread_reply


async def handle_message(bot, message: discord.Message):
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    if message.author.bot:
        return
    if not allowed(message.author.id):
        return

    if isinstance(message.channel, discord.Thread):
        await handle_thread_reply(bot, message)
