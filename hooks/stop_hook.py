#!/usr/bin/env python3
"""Stop-event hook (https://antigravity.google/docs/hooks#stop).

`fullyIdle: false` means a run_command call detached to async (its
WaitMsBeforeAsync budget ran out) is still in flight. Returning
{"decision": "continue"} keeps the turn alive so agy can pick up that
result instead of ending the turn with it lost. Capped by executionNum
so a genuinely stuck command doesn't loop forever.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

MAX_CONTINUE_ATTEMPTS = 20
# This runs as its own process, invoked directly by agy - not visible in
# `lgy logs`. Plain-file logging is the only way to inspect it.
DEBUG_LOG = Path.home() / ".gemini" / "linkgravity" / "logs" / "stop_hook_debug.log"


def log(line: str):
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {line}\n")
    except Exception:
        pass


def main():
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
    except Exception as e:
        log(f"[PARSE ERROR] {e} raw={raw!r}")
        print(json.dumps({}))
        return

    fully_idle = hook_input.get("fullyIdle", True)
    execution_num = hook_input.get("executionNum", 0)
    termination_reason = hook_input.get("terminationReason")
    log(
        f"[STOP HOOK] fullyIdle={fully_idle!r} executionNum={execution_num!r} "
        f"terminationReason={termination_reason!r} conv={hook_input.get('conversationId')!r}"
    )

    if not fully_idle and execution_num < MAX_CONTINUE_ATTEMPTS:
        response = {
            "decision": "continue",
            "reason": (
                "A background command is still running. Wait for it to finish, "
                "then report its actual result to the user before ending your turn."
            ),
        }
        log(f"[STOP HOOK] -> continue: {response}")
        print(json.dumps(response))
    else:
        log("[STOP HOOK] -> {} (fullyIdle true or attempt cap reached)")
        print(json.dumps({}))


if __name__ == "__main__":
    main()
