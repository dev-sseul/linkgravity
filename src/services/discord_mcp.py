import os

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("DiscordButtons")


@mcp.tool()
def ask_discord_user(question: str, options: list[str]) -> str:
    """
    Ask a multiple-choice question to the user in Discord using interactive buttons.
    You MUST use this tool instead of the default ask_question tool when running in Discord.
    """
    thread_id = os.environ.get("DISCORD_THREAD_ID")
    if not thread_id:
        return "Error: DISCORD_THREAD_ID not set. Are you running in Discord?"

    try:
        resp = requests.post(
            "http://127.0.0.1:18080/mcp_ask",
            json={"thread_id": thread_id, "question": question, "options": options},
            timeout=300,
        )
        if resp.status_code == 200:
            return resp.json().get("answer", "No answer")
        return f"Error: HTTP {resp.status_code}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def send_discord_message(channel_id: str, message: str) -> str:
    """
    Sends a text message to a specific Discord channel by its ID.
    You can use this tool when the user asks you to send a message to a different channel.
    """
    try:
        resp = requests.post(
            "http://127.0.0.1:18080/mcp_send_channel", json={"channel_id": channel_id, "message": message}, timeout=10
        )
        if resp.status_code == 200:
            return "Message sent successfully"
        return f"Error: HTTP {resp.status_code} - {resp.text}"
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
