"""Core messenger interface. Every backend (Discord now, Slack/Telegram
later) implements MessengerAdapter; business logic never touches
platform SDK types directly.

Futures for approval/question prompts are owned by business logic, not
the adapter - the same future can also be resolved by a typed reply,
voice, or /stop, not just a button. Voice is deliberately not part of
this interface; see VoiceCapable.
"""

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class ScopeOption:
    """kind: "commands" or "tools" - which persistent allowlist scope belongs to."""

    kind: str
    scope: str


@dataclass
class ToolApprovalOutcome:
    decision: str  # "allow" | "reject"
    scope: ScopeOption | None = None


class PromptHandle(ABC):
    outcome: ToolApprovalOutcome | None = None

    @abstractmethod
    async def send(self, conversation_ref: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def finalize(self) -> None:
        raise NotImplementedError


class MessengerAdapter(ABC):
    platform_name: str = "unknown"

    @abstractmethod
    async def send_message(self, conversation_ref: Any, text: str) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def edit_message(self, message_ref: Any, text: str) -> bool:
        """Returns False if the message is gone - caller should send a new one instead."""
        raise NotImplementedError

    @abstractmethod
    async def send_files(self, conversation_ref: Any, file_paths: list[str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def resolve_conversation(self, conversation_id: str) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def start_conversation(self, origin_ref: Any, title: str) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def rename_conversation(self, conversation_ref: Any, title: str) -> None:
        raise NotImplementedError

    @asynccontextmanager
    async def typing(self, conversation_ref: Any):
        yield

    @abstractmethod
    def create_tool_approval_prompt(
        self,
        decision_future,
        title: str,
        body: str,
        scope_options: list[ScopeOption],
    ) -> PromptHandle:
        raise NotImplementedError

    @abstractmethod
    def create_question_prompt(
        self,
        answer_future,
        question: str,
        options: list[str],
        multi_select: bool = False,
        allow_write_in: bool = True,
    ) -> PromptHandle:
        raise NotImplementedError

    @property
    def supports_voice(self) -> bool:
        return isinstance(self, VoiceCapable)


@runtime_checkable
class VoiceCapable(Protocol):
    async def join_voice(self, guild_ref: Any, channel_ref: Any) -> None: ...
    async def leave_voice(self, guild_ref: Any) -> None: ...
    async def play_tts(self, guild_ref: Any, audio_bytes: bytes) -> None: ...
