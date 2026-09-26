import json
import re
from pathlib import PurePosixPath

from approval.mcp_tools import unquote

# Shorter timers are how agy waits on its own background work within a turn, so they are left alone.
MAX_TIMER_SEC = 300
SIDECAR_DIR = "/.gemini/config/sidecars/"
_DESCRIPTORS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_GO_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _timing(cron: str) -> dict | None:
    cron = cron.strip()
    if cron in _DESCRIPTORS:
        return {"cron": _DESCRIPTORS[cron]}
    if cron.startswith("@every "):
        m = _GO_DURATION.match(cron[len("@every ") :].strip())
        if not m or not any(m.groups()):
            return None
        h, mi, sec = (int(g or 0) for g in m.groups())
        return {"every": f"{max(1, h * 60 + mi + round(sec / 60))}m"}
    return {"cron": cron}


def _from_schedule_tool(tool_input: dict) -> dict | None:
    prompt = unquote(tool_input.get("Prompt"))
    cron = unquote(tool_input.get("CronExpression"))
    if cron:
        timing = _timing(cron)
    else:
        try:
            seconds = float(unquote(tool_input.get("DurationSeconds")) or 0)
        except ValueError:
            return None
        if seconds <= MAX_TIMER_SEC:
            return None
        timing = {"delay": f"{round(seconds / 60)}m"}
    return timing and prompt and {**timing, "prompt": prompt}


def _from_sidecar(tool_input: dict) -> dict | None:
    target = unquote(tool_input.get("TargetFile"))
    if SIDECAR_DIR not in target or not target.endswith("/sidecar.json"):
        return None
    try:
        sidecar = json.loads(unquote(tool_input.get("CodeContent")))
    except ValueError:
        return None
    args = sidecar.get("args") or []
    # The automation skill's shape: ["<cron>", "agentapi", "new-conversation", "<prompt>"].
    if sidecar.get("builtin") != "schedule" or len(args) < 4 or args[1] != "agentapi":
        return None
    timing = _timing(str(args[0]))
    name = sidecar.get("description") or PurePosixPath(target).parent.name
    return timing and {**timing, "prompt": str(args[-1]), "name": name}


def to_schedule_args(tool_name: str, tool_input: dict) -> dict | None:
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "schedule":
        return _from_schedule_tool(tool_input)
    if tool_name == "write_to_file":
        return _from_sidecar(tool_input)
    return None


def killed_task_id(tool_name: str, tool_input: dict) -> str | None:
    # Adopted schedules are reported to the model as "#id", so it cancels them the agy way.
    if tool_name != "manage_task" or not isinstance(tool_input, dict):
        return None
    if unquote(tool_input.get("Action")).lower() != "kill":
        return None
    return unquote(tool_input.get("TaskId")).lstrip("#") or None
