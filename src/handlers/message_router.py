from config import allowed
from handlers.thread_reply import handle_thread_reply
from messengers.base import MessengerAdapter


async def handle_message(bot, raw_event, adapter: MessengerAdapter):
    incoming = adapter.to_incoming_message(raw_event)
    if incoming is None:
        return
    if not allowed(incoming.author_id, incoming.platform):
        return

    await handle_thread_reply(bot, incoming)
