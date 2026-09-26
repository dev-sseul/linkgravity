import json


def unquote(value) -> str:
    # agy sometimes hands tool arguments over still JSON-encoded ("\"linkgravity\"").
    text = str(value if value is not None else "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            return str(json.loads(text))
        except ValueError:
            return text.strip('"')
    return text


def is_linkgravity_tool(tool_name: str, tool_input: dict, name: str) -> bool:
    # agy lazy-loads MCP servers, so their tools usually arrive wrapped in call_mcp_tool.
    if tool_name == "call_mcp_tool":
        return unquote(tool_input.get("ServerName")) == "linkgravity" and unquote(tool_input.get("ToolName")) == name
    return tool_name == f"mcp_linkgravity_{name}"
