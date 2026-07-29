"""Per-platform messenger adapter registry, populated once at startup by
main.py. Both bots now share one process, so callers must say which
platform they mean - either directly (voice/Discord-only code) or by
looking up which platform a given thread_id/conversation belongs to."""

from messengers.base import MessengerAdapter

_adapters: dict[str, MessengerAdapter] = {}


def register_adapter(platform: str, adapter: MessengerAdapter) -> None:
    _adapters[platform] = adapter


def get_adapter_for_platform(platform: str) -> MessengerAdapter:
    if platform not in _adapters:
        raise RuntimeError(f"No adapter registered for platform {platform!r} - is it enabled in lgy.json?")
    return _adapters[platform]


def get_adapter_for_thread(thread_id: str) -> MessengerAdapter:
    from config import session_manager

    session = session_manager.get_session(thread_id) or {}
    platform = session.get("platform", "discord")  # pre-multi-platform sessions have no tag - assume discord
    return get_adapter_for_platform(platform)


def get_adapter_for_conv_id(conv_id: str) -> MessengerAdapter | None:
    from config import session_manager

    for _thread_id, sess in session_manager.get_all_sessions().items():
        if sess.get("conversation_id") == conv_id:
            return get_adapter_for_platform(sess.get("platform", "discord"))
    return None
