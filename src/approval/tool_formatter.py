import json
import os

TOOL_DISPLAY_NAMES = {
    "view_file": "Read",
    "write_to_file": "Write",
    "replace_file_content": "Edit",
    "multi_replace_file_content": "Edit",
    "grep_search": "Grep",
    "list_dir": "List",
}

PATH_ARG_KEYS = ["AbsolutePath", "TargetFile", "DirectoryPath"]

DETAIL_FIELD_KEYS = {"Description", "Instruction", "TargetFile", "AbsolutePath", "DirectoryPath", "Query", "SearchPath"}


def format_tool_display(tool_name: str, tool_input: dict) -> tuple[str, str, dict]:
    """
    Formats the tool name and input into user-friendly display text.
    Returns: (tool_msg_text, desc_json, view_tool_input)
    """
    display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)

    args_str = None
    for key in PATH_ARG_KEYS:
        if key in tool_input:
            args_str = os.path.basename(tool_input[key])
            break
    if args_str is None:
        args_str = ", ".join(f"{k}={v}" for k, v in tool_input.items() if len(str(v)) < 50)

    tool_msg_text = f"● {display_name}({args_str})"
    view_tool_input = tool_input

    fields_text = ""
    for k, v in tool_input.items():
        if k in DETAIL_FIELD_KEYS:
            fields_text += f"**{k}**: {v}\n"

    code_text = ""
    if "CodeContent" in tool_input:
        code_text = f"\n**Code Content:**\n```python\n{tool_input['CodeContent'][:1000]}\n```"
    elif "ReplacementChunks" in tool_input:
        for i, chunk in enumerate(tool_input["ReplacementChunks"]):
            code_text += f"\n**Replacement Chunk #{i + 1} (Lines {chunk.get('StartLine')}-{chunk.get('EndLine')}):**\n"
            code_text += f"```python\n{chunk.get('ReplacementContent')[:500]}\n```"
    elif "ReplacementContent" in tool_input:
        code_text += f"\n**Replacement Content:**\n```python\n{tool_input['ReplacementContent'][:1000]}\n```"

    if fields_text or code_text:
        desc_json = f"\n{fields_text}{code_text}"
    else:
        desc_json = f"\n```json\n{json.dumps(tool_input, indent=2, ensure_ascii=False)[:1000]}\n```"

    return tool_msg_text, desc_json, view_tool_input


def format_bash_display(sub_cmd: str) -> tuple[str, str, dict]:
    """
    Formats a single bash sub-command for display.
    """
    is_long = "\n" in sub_cmd or len(sub_cmd) > 50
    display_cmd = sub_cmd.split("\n")[0][:50] + "..." if is_long else sub_cmd
    tool_msg_text = f"● Bash({display_cmd})"
    view_tool_input = {"CommandLine": sub_cmd}
    desc_json = f"\n```bash\n{sub_cmd}\n```" if is_long else ""
    return tool_msg_text, desc_json, view_tool_input
