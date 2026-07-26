from config import WORKSPACE_DIR
from core.agy_runner import (
    active_processes,
    agy_new_conversation,
    agy_send_message,
    agy_start_lock,
    generate_thread_title,
    get_current_model,
    run_agy,
)
from services.audio_service import stt, tts
from services.discord_helpers import (
    build_content_with_images,
    check_approval_intent,
    clean_ansi,
    cleanup_images,
    handle_image_attachments,
)

__all__ = [
    "run_agy",
    "agy_new_conversation",
    "agy_send_message",
    "get_current_model",
    "generate_thread_title",
    "active_processes",
    "agy_start_lock",
    "tts",
    "stt",
    "handle_image_attachments",
    "cleanup_images",
    "build_content_with_images",
    "clean_ansi",
    "check_approval_intent",
    "get_default_cwd",
]


def get_default_cwd(folder_name="workspace"):
    return str(WORKSPACE_DIR / folder_name)
