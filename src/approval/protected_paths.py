import os
import re
from pathlib import Path

from config import WORKSPACE_DIR

AGENT_SUBDIR = "workspace"

_DIR_MARKER = f"{WORKSPACE_DIR.parent.name}/{WORKSPACE_DIR.name}"

# Shell commands arrive as one opaque string, so they are matched textually rather than resolved.
CONFIG_DIR_RE = re.compile(re.escape(_DIR_MARKER) + rf"(?!/{re.escape(AGENT_SUBDIR)}\b)")

NAME_RE = re.compile(r"(?<![\w.-])(?:lgy\.json|persistent_tools\.json|approve_token)(?![\w.])")

REASON = "LinkGravity's own configuration is off limits - it holds the bot tokens."


def _is_inside_config(value: str) -> bool:
    try:
        resolved = Path(os.path.expandvars(os.path.expanduser(value))).resolve()
    except (OSError, ValueError):
        return False
    root = WORKSPACE_DIR.resolve()
    return resolved.is_relative_to(root) and not resolved.is_relative_to(root / AGENT_SUBDIR)


def protected_reason(tool_input) -> str | None:
    if isinstance(tool_input, list):
        return next((r for r in map(protected_reason, tool_input) if r), None)
    if not isinstance(tool_input, dict):
        return None

    for value in tool_input.values():
        if isinstance(value, (dict, list)):
            nested = protected_reason(value)
            if nested:
                return nested
        elif isinstance(value, str) and value:
            if NAME_RE.search(value) or CONFIG_DIR_RE.search(value) or _is_inside_config(value):
                return REASON
    return None
