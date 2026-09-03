from config import session_manager
from messengers.base import PermissionEntry

# Discord allows 25 components per message; the rest is headroom for paging buttons.
PAGE_SIZE = 20


def set_auto_mode(enabled: bool) -> str:
    session_manager.set_auto_mode(enabled)
    if enabled:
        return (
            "⚠️ **Auto-mode ON** — every tool/command approval will be auto-allowed globally, "
            "across all sessions and platforms, until you run `/automode off`. Protected paths "
            "are still blocked regardless."
        )
    return "🔒 Auto-mode OFF — approvals are back to normal."


def list_entries() -> list[PermissionEntry]:
    allowed = session_manager.persistent_allowed
    return [
        PermissionEntry(kind=kind, scope=scope) for kind in ("tools", "commands") for scope in allowed.get(kind) or []
    ]


def revoke(entry: PermissionEntry) -> bool:
    bucket = session_manager.persistent_allowed.get(entry.kind) or []
    if entry.scope not in bucket:
        return False
    bucket.remove(entry.scope)
    session_manager.save_persistent()
    return True


def page_of(entries: list[PermissionEntry], page: int) -> tuple[list[PermissionEntry], int, int]:
    total_pages = max(1, -(-len(entries) // PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    return entries[start : start + PAGE_SIZE], page, total_pages


def entry_label(entry: PermissionEntry) -> str:
    suffix = " (tool)" if entry.kind == "tools" else ""
    return f"{entry.scope}{suffix}"


def render_body(entries: list[PermissionEntry], page: int, total_pages: int) -> str:
    if not entries:
        return "No permissions have been allowed yet."
    lines = [f"• {entry_label(e)}" for e in entries]
    if total_pages > 1:
        lines.append(f"\nPage {page + 1} of {total_pages}")
    return "\n".join(lines)
