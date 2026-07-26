"""Process-wide messenger adapter instance, set once at startup by
main.py. Matches the codebase's existing singleton pattern (see
config.session_manager) so deep call sites don't need the adapter
threaded through every signature."""

from messengers.base import MessengerAdapter

_adapter: MessengerAdapter | None = None


def set_adapter(adapter: MessengerAdapter) -> None:
    global _adapter
    _adapter = adapter


def get_adapter() -> MessengerAdapter:
    if _adapter is None:
        raise RuntimeError("Messenger adapter not initialized - main.py must call set_adapter() at startup.")
    return _adapter
