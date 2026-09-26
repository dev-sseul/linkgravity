import re
import shlex
from pathlib import PurePosixPath

from approval.command_parser import parse_shell_commands
from approval.mcp_tools import is_linkgravity_tool, unquote

TOOLS = {"view_file", "list_dir", "read_file", "search_web", "read_url_content"}
MANAGE_TASK_ACTIONS = {"list", "status"}
LINKGRAVITY_TOOLS = ("send_attachment", "schedule_list")

COMMANDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "wc", "pwd", "date", "df", "du", "free",
    "uptime", "ps", "sensors", "whoami", "hostname", "uname", "id", "which", "file", "stat", "find",
    "sort", "uniq", "cut", "tr", "echo", "printf", "jq", "lsblk", "nvidia-smi",
    "docker ps", "docker images", "docker logs", "docker stats", "docker inspect",
    "git status", "git log", "git diff", "git show", "systemctl status",
}  # fmt: skip

# Flags that write, run other commands, or never exit (a scheduled run would hang on them).
BLOCKED_FLAGS = {
    "tail": {"-f", "-F", "--follow"},
    "docker logs": {"-f", "--follow"},
    "find": {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"},
    "sort": {"-o", "--output"},
    "git diff": {"--output"},
    "date": {"-s", "--set"},
}

_HARMLESS_REDIRECT = re.compile(r"\s*(?:[12]?>&[12]|&>\s*/dev/null|[12]?>\s*/dev/null)")
_SUBSTITUTION = ("`", "$(", "<(", ">(")


def _command_allowed(sub_cmd: str) -> bool:
    try:
        tokens = shlex.split(sub_cmd)
    except ValueError:
        return False
    if not tokens or "=" in tokens[0]:
        return False
    tokens[0] = PurePosixPath(tokens[0]).name
    name = " ".join(tokens[:2]) if " ".join(tokens[:2]) in COMMANDS else tokens[0]
    if name not in COMMANDS:
        return False
    args = tokens[len(name.split()) :]
    blocked = BLOCKED_FLAGS.get(name, set())
    if any(a in blocked or a.split("=", 1)[0] in blocked for a in args):
        return False
    if name == "docker stats" and "--no-stream" not in args:
        return False
    # uniq's second positional argument is an output file.
    return not (name == "uniq" and len([a for a in args if not a.startswith("-")]) > 1)


def command_allowed(command_line: str) -> bool:
    if any(s in command_line for s in _SUBSTITUTION):
        return False
    stripped = _HARMLESS_REDIRECT.sub("", command_line).replace("&&", " ")
    # Checked on the whole line, since the parser keeps these inside a sub-command.
    if ">" in stripped or "&" in stripped:
        return False
    sub_cmds = parse_shell_commands(command_line)
    return bool(sub_cmds) and all(_command_allowed(_HARMLESS_REDIRECT.sub("", c)) for c in sub_cmds)


def allows(tool_name: str, tool_input: dict) -> bool:
    if not isinstance(tool_input, dict):
        return False
    if tool_name in TOOLS:
        return True
    if tool_name == "manage_task":
        return unquote(tool_input.get("Action")).lower() in MANAGE_TASK_ACTIONS
    if tool_name == "run_command":
        return command_allowed(unquote(tool_input.get("CommandLine")))
    return any(is_linkgravity_tool(tool_name, tool_input, t) for t in LINKGRAVITY_TOOLS)
