#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request


def main():
    if os.environ.get("AGY_DISCORD_BOT") != "1":
        print(json.dumps({"decision": "allow"}))
        return

    try:
        raw_input = sys.stdin.read()
        hook_input = json.loads(raw_input)
    except Exception:
        print(json.dumps({"decision": "deny", "reason": "Failed to parse hook input."}))
        return

    tool_call = hook_input.get("toolCall", {})
    tool_name = tool_call.get("name", "unknown_tool")

    tool_input_data = tool_call.get("args", {})
    conv_id = hook_input.get("conversationId", "unknown")

    payload = json.dumps(
        {
            "conversation_id": conv_id,
            "tool_name": tool_name,
            "tool_input": tool_input_data,
            "thread_id": os.environ.get("DISCORD_THREAD_ID"),
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "http://localhost:18080/approve", data=payload, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=3600) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            decision = res_data.get("decision", "allow")
            if decision == "allow":
                out = {"decision": "allow"}
                # Print mode requires a matching allow rule even when this
                # hook says "allow", or it soft-denies the tool call anyway.
                if res_data.get("permissionOverrides"):
                    out["permissionOverrides"] = res_data["permissionOverrides"]
                print(json.dumps(out))
            else:
                reason = res_data.get("reason", "User rejected the action.")
                print(json.dumps({"decision": "deny", "reason": reason}))
    except Exception as e:
        sys.stderr.write(f"Hook error: {e}\n")
        print(json.dumps({"decision": "deny", "reason": f"Connection to webhook failed: {e}"}))


if __name__ == "__main__":
    main()
