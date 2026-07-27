from config import allowed
from handlers.thread_reply import handle_thread_reply
from messengers.registry import get_adapter


async def handle_message(bot, raw_event):
    incoming = get_adapter().to_incoming_message(raw_event)
    if incoming is None:
        return
    if not allowed(incoming.author_id):
        return

    await handle_thread_reply(bot, incoming)
