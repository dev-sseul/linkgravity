import asyncio
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_json, safe_load_json


class SessionManager:
    """Manages conversation state, async streaming queues, and user approval states."""

    def __init__(self, data_dir: Path):
        self.session_file = data_dir / "sessions.json"
        self.persistent_file = data_dir / "persistent_tools.json"

        self.sessions: dict[str, dict] = self._load_sessions()
        self.active_queues: dict[str, asyncio.Queue] = {}
        self.active_tts_tasks: dict[str, asyncio.Task] = {}
        self.pending_approvals: dict[str, asyncio.Future] = {}
        self.pending_approval_types: dict[str, str] = {}
        self.pending_approval_messages: dict[str, Any] = {}
        # conv_id -> current approval_key. Keyed by approval_key (not conv_id)
        # so a 2nd call can't overwrite the 1st's still-pending Future.
        self.active_approval_by_conv: dict[str, str] = {}

        self.persistent_allowed: dict = self._load_persistent()
        self.session_allowed_tools: dict[str, set] = {}

    def _load_sessions(self) -> dict:
        from config import logger

        return safe_load_json(self.session_file, {}, logger=logger)

    def save_sessions(self):
        atomic_write_json(self.session_file, self.sessions)

    def get_session(self, thread_id: str) -> dict | None:
        return self.sessions.get(str(thread_id))

    def set_session(self, thread_id: str, data: dict):
        self.sessions[str(thread_id)] = data
        self.save_sessions()

    def update_session(self, thread_id: str, key: str, value: Any):
        if str(thread_id) in self.sessions:
            self.sessions[str(thread_id)][key] = value
            self.save_sessions()

    def remove_session(self, thread_id: str) -> dict | None:
        sess = self.sessions.pop(str(thread_id), None)
        self.save_sessions()
        return sess

    def get_all_sessions(self) -> dict[str, dict]:
        return self.sessions

    def _load_persistent(self) -> dict:
        from config import logger

        data = safe_load_json(self.persistent_file, {"tools": [], "commands": []}, logger=logger)
        if isinstance(data, list):
            return {"tools": data, "commands": []}
        return data

    def save_persistent(self):
        atomic_write_json(self.persistent_file, self.persistent_allowed)

    def register_queue(self, thread_id: str, queue: asyncio.Queue):
        self.active_queues[str(thread_id)] = queue

    def remove_queue(self, thread_id: str) -> asyncio.Queue | None:
        return self.active_queues.pop(str(thread_id), None)

    def get_queue(self, thread_id: str) -> asyncio.Queue | None:
        return self.active_queues.get(str(thread_id))

    def has_active_queues(self) -> bool:
        return bool(self.active_queues)

    def get_active_queue_keys(self) -> list:
        return list(self.active_queues.keys())

    def get_tts_task(self, thread_id: str) -> asyncio.Task | None:
        return self.active_tts_tasks.get(str(thread_id))

    def set_tts_task(self, thread_id: str, task: asyncio.Task):
        self.active_tts_tasks[str(thread_id)] = task

    def remove_tts_task(self, thread_id: str):
        self.active_tts_tasks.pop(str(thread_id), None)

    def set_pending_approval(
        self, approval_key: str, future: asyncio.Future, app_type: str = "tool", conv_id: str | None = None
    ):
        self.pending_approvals[approval_key] = future
        self.pending_approval_types[approval_key] = app_type
        if conv_id:
            self.active_approval_by_conv[conv_id] = approval_key

    def get_pending_approval(self, approval_key: str) -> asyncio.Future | None:
        return self.pending_approvals.get(approval_key)

    def get_pending_approval_by_conv(self, conv_id: str) -> asyncio.Future | None:
        """Looks up whichever approval is CURRENTLY active for a given
        conversation - for callers that only have the stable
        conversation_id, not the specific per-call approval_key (voice/
        text "yes"/"no" responses, /stop)."""
        approval_key = self.active_approval_by_conv.get(conv_id)
        if not approval_key:
            return None
        return self.pending_approvals.get(approval_key)

    def get_pending_approval_type_by_conv(self, conv_id: str) -> str:
        approval_key = self.active_approval_by_conv.get(conv_id)
        if not approval_key:
            return "tool"
        return self.pending_approval_types.get(approval_key, "tool")

    def clear_pending_approval(self, approval_key: str):
        self.pending_approvals.pop(approval_key, None)
        self.pending_approval_types.pop(approval_key, None)
        self.pending_approval_messages.pop(approval_key, None)
        # Only remove the conv_id pointer if it still points at THIS key -
        # a newer call may have already overwritten it.
        for conv, key in list(self.active_approval_by_conv.items()):
            if key == approval_key:
                del self.active_approval_by_conv[conv]
