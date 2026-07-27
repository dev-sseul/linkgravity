import asyncio
import json
import re
import shlex
import uuid

from aiohttp import web

from config import MAX_EMBED_LEN, logger, session_manager
from messengers.base import ScopeOption
from messengers.registry import get_adapter


def is_tool_allowed(tool_name, tool_input):
    from api.server import is_tool_allowed as is_tool_allowed_orig

    return is_tool_allowed_orig(tool_name, tool_input)


async def send_ordered(target_thread_id, send_coro_factory):
    """Routes through the same per-conversation stream queue as the answer
    text, so tool-call messages can't arrive out of order. Falls back to
    a direct call if no stream is registered."""
    q = session_manager.get_queue(target_thread_id) if target_thread_id else None
    if not q:
        return await send_coro_factory()

    from config import STREAM_RATE_LIMIT_SEC

    await asyncio.sleep(STREAM_RATE_LIMIT_SEC)

    done = asyncio.get_running_loop().create_future()
    q.put_nowait(("__RUN_ORDERED__", send_coro_factory, done))
    return await done


def build_permission_overrides(tool_name, tool_input):
    """Builds the PreToolUse `permissionOverrides` output field for an
    approved call. Print mode soft-denies a tool unless a matching allow
    rule exists even when the hook says "allow"; returning
    "command(<CommandLine>)" supplies that rule for this one call."""
    if tool_name == "run_command" and isinstance(tool_input, dict):
        command_line = tool_input.get("CommandLine")
        if command_line:
            return [f"command({command_line})"]
    return None


def allow_response(tool_name, tool_input):
    body = {"decision": "allow"}
    overrides = build_permission_overrides(tool_name, tool_input)
    if overrides:
        body["permissionOverrides"] = overrides
    return web.json_response(body)


def _persist_scope_if_granted(prompt_handle):
    """If the prompt was resolved via a persistent-allow button, records
    that scope. Scope persistence is business logic, so it lives here
    rather than inside the adapter's button callback - a future resolved
    by a non-UI path (typed reply, voice) simply has no outcome/scope."""
    outcome = prompt_handle.outcome
    if outcome and outcome.decision == "allow" and outcome.scope:
        kind, scope = outcome.scope.kind, outcome.scope.scope
        if scope not in session_manager.persistent_allowed[kind]:
            session_manager.persistent_allowed[kind].append(scope)
            session_manager.save_persistent()


def _clean_inline(text: str) -> str:
    return re.sub(r"[*#_`]", "", text).replace("\n", " ").strip()


async def handle_approve_request(request):
    # Tracks approval_keys registered this request so the exception handler can clean them up.
    registered_approval_keys = []
    try:
        data = await request.json()
        conv_id = data.get("conversation_id")
        tool_name = data.get("tool_name")
        tool_input = data.get("tool_input")
        payload_thread_id = data.get("thread_id")

        # DEBUG-only (see logger.py's LOG_LEVEL). Silent by default.
        logger.debug(f"[APPROVE HOOK] tool_name={tool_name!r} conv_id={conv_id!r} tool_input={tool_input!r}")

        adapter = get_adapter()

        target_thread = None
        target_thread_id = None
        for thread_id_str, sess in session_manager.get_all_sessions().items():
            if sess.get("conversation_id") == conv_id:
                target_thread = adapter.resolve_conversation(thread_id_str)
                target_thread_id = thread_id_str
                break

        if not target_thread and payload_thread_id:
            resolved_channel = adapter.resolve_conversation(payload_thread_id)
            if resolved_channel:
                target_thread = resolved_channel
                target_thread_id = payload_thread_id
                if session_manager.get_session(payload_thread_id):
                    session_manager.update_session(payload_thread_id, "conversation_id", conv_id)
                    session_manager.update_session(payload_thread_id, "status", "active")

        if not target_thread:
            for thread_id_str, sess in reversed(list(session_manager.get_all_sessions().items())):
                if sess.get("status") == "pending":
                    target_thread = adapter.resolve_conversation(thread_id_str)
                    session_manager.update_session(thread_id_str, "conversation_id", conv_id)
                    session_manager.update_session(thread_id_str, "status", "active")
                    target_thread_id = thread_id_str
                    break

        def _set_tool_status(key: str):
            if target_thread_id and session_manager.get_session(target_thread_id):
                session_manager.update_session(target_thread_id, key, tool_name)

        if "ask_question" in tool_name:
            _set_tool_status("current_tool")  # no separate approval phase here - it's waiting on the user either way
            if not target_thread:
                return web.json_response({"decision": "deny", "reason": "No target thread found."})

            questions = tool_input.get("questions", [])
            if not questions:
                return web.json_response({"decision": "deny", "reason": "No questions provided."})

            q_data = questions[0]
            question_text = _clean_inline(q_data.get("question", "No question provided."))
            options = [(_clean_inline(str(opt)) or "Option")[:80] for opt in q_data.get("options", [])]
            is_multi_select = q_data.get("is_multi_select", False)

            future = asyncio.get_running_loop().create_future()
            approval_key = f"{conv_id}:{uuid.uuid4().hex}"
            session_manager.set_pending_approval(approval_key, future, "ask_question", conv_id=conv_id)
            registered_approval_keys.append(approval_key)

            prompt = adapter.create_question_prompt(
                future, question_text, options, multi_select=is_multi_select, allow_write_in=True
            )
            await send_ordered(target_thread_id, lambda: prompt.send(target_thread))

            chosen_opt = await future
            session_manager.clear_pending_approval(approval_key)
            await prompt.finalize()

            return web.json_response(
                {"decision": "deny", "reason": f"User selected via Discord button: [{chosen_opt}]"}
            )

        from approval.command_parser import parse_shell_commands
        from approval.tool_formatter import format_bash_display, format_tool_display

        if "run_command" in tool_name:
            cmd = tool_input.get("CommandLine", "")
            sub_cmds = parse_shell_commands(cmd)
            tool_msg_text, desc_json, _ = format_bash_display(sub_cmds[0] if sub_cmds else cmd)
            tool_msg_formatted = f"```text\n{tool_msg_text}\n```{desc_json}"
        else:
            sub_cmds = [None]
            tool_msg_text, desc_json, view_tool_input = format_tool_display(tool_name, tool_input)
            tool_msg_formatted = f"```text\n{tool_msg_text}\n```{desc_json}"

        if "run_command" in tool_name:
            prompted = False
            for sub_cmd in sub_cmds:
                if not sub_cmd:
                    continue

                is_auto_allowed = False
                if "\n" not in sub_cmd and "|" not in sub_cmd:
                    try:
                        tokens = shlex.split(sub_cmd)
                        for scope in session_manager.persistent_allowed.get("commands", []):
                            scope_tokens = shlex.split(scope)
                            if len(scope_tokens) <= len(tokens) and tokens[: len(scope_tokens)] == scope_tokens:
                                is_auto_allowed = True
                                break
                    except ValueError:
                        pass
                elif is_tool_allowed(tool_name, {"CommandLine": sub_cmd}) or (
                    conv_id in session_manager.session_allowed_tools
                    and tool_name in session_manager.session_allowed_tools[conv_id]
                ):
                    is_auto_allowed = True

                if is_auto_allowed:
                    continue

                prompted = True

                prompt_desc = f"**🎯 Requesting permission for:**\n```bash\n{sub_cmd}\n```\n"
                if sub_cmd.strip() != cmd.strip():
                    prompt_desc += f"**📜 Full command context:**\n```bash\n{cmd}\n```"
                prompt_desc += (
                    f"\n**🔧 Tool Execution Detail:**\n```json\n"
                    f"{json.dumps(tool_input, indent=2, ensure_ascii=False)[:1000]}\n```"
                )

                # Persistent-allow options: expanding prefixes of the sub-command's tokens.
                scope_options = []
                try:
                    tokens = shlex.split(sub_cmd)
                except ValueError:
                    tokens = [sub_cmd]
                current_prefix = []
                for t in tokens[:3]:
                    current_prefix.append(t)
                    scope_options.append(ScopeOption(kind="commands", scope=" ".join(current_prefix)))

                approval_key = f"{conv_id}:{uuid.uuid4().hex}"
                future = asyncio.get_running_loop().create_future()
                session_manager.set_pending_approval(approval_key, future, conv_id=conv_id)
                registered_approval_keys.append(approval_key)

                prompt = adapter.create_tool_approval_prompt(
                    future, "⚠️ Tool Execution Approval Required", prompt_desc, scope_options
                )

                sub_cmd_display, sub_cmd_desc, _ = format_bash_display(sub_cmd)
                sub_cmd_formatted = f"```text\n{sub_cmd_display}\n```{sub_cmd_desc}"

                async def _send_bash_prompt(sub_cmd_formatted=sub_cmd_formatted, prompt=prompt):
                    await adapter.send_message(target_thread, sub_cmd_formatted)
                    return await prompt.send(target_thread)

                await send_ordered(target_thread_id, _send_bash_prompt)
                _set_tool_status("pending_approval_tool")

                decision = await future
                session_manager.clear_pending_approval(approval_key)
                _persist_scope_if_granted(prompt)
                await prompt.finalize()

                if decision == "reject":
                    return web.json_response({"decision": "reject"})

                _set_tool_status("current_tool")

            if target_thread and tool_msg_text and not prompted:
                await send_ordered(target_thread_id, lambda: adapter.send_message(target_thread, tool_msg_formatted))

            if not prompted:
                _set_tool_status("current_tool")  # auto-allowed - runs immediately, no approval wait

            return allow_response(tool_name, tool_input)

        else:
            is_auto_allowed = False
            if is_tool_allowed(tool_name, tool_input) or (
                conv_id in session_manager.session_allowed_tools
                and tool_name in session_manager.session_allowed_tools[conv_id]
            ):
                is_auto_allowed = True

            if is_auto_allowed:
                if target_thread and tool_msg_text:
                    await send_ordered(
                        target_thread_id, lambda: adapter.send_message(target_thread, tool_msg_formatted)
                    )
                _set_tool_status("current_tool")  # auto-allowed - runs immediately, no approval wait
                return allow_response(tool_name, tool_input)

            approval_key = f"{conv_id}:{uuid.uuid4().hex}"
            future = asyncio.get_running_loop().create_future()
            session_manager.set_pending_approval(approval_key, future, conv_id=conv_id)
            registered_approval_keys.append(approval_key)

            scope_options = [ScopeOption(kind="tools", scope=tool_name)]
            prompt = adapter.create_tool_approval_prompt(
                future, "⚠️ Tool Execution Approval Required", tool_msg_formatted, scope_options
            )

            async def _send_prompt():
                await adapter.send_message(target_thread, tool_msg_formatted)
                return await prompt.send(target_thread)

            await send_ordered(target_thread_id, _send_prompt)
            _set_tool_status("pending_approval_tool")

            decision = await future
            session_manager.clear_pending_approval(approval_key)
            _persist_scope_if_granted(prompt)
            await prompt.finalize()

            if decision == "reject":
                return web.json_response({"decision": "reject"})

            _set_tool_status("current_tool")
            return allow_response(tool_name, tool_input)

    except Exception as e:
        logger.exception(f"Error in handle_approve_request: {e}")
        for key in registered_approval_keys:
            session_manager.clear_pending_approval(key)
        return web.json_response({"decision": "allow"})


async def handle_mcp_ask(request):
    try:
        data = await request.json()
        thread_id = data.get("thread_id")

        question = _clean_inline(data.get("question", "No question provided."))
        options = [(_clean_inline(str(opt)) or "Option")[:80] for opt in data.get("options", [])]

        adapter = get_adapter()
        thread = adapter.resolve_conversation(thread_id)
        if not thread:
            return web.json_response({"answer": "Thread not found"}, status=400)

        future = asyncio.get_event_loop().create_future()
        # conv_id is always None here - key just needs to be unique for cleanup.
        approval_key = f"mcp_ask:{thread_id}:{uuid.uuid4().hex}"
        session_manager.set_pending_approval(approval_key, future, "ask_question")

        prompt = adapter.create_question_prompt(future, question, options, allow_write_in=True)
        msg = await prompt.send(thread)
        session_manager.pending_approval_messages[approval_key] = msg

        try:
            answer = await asyncio.wait_for(future, timeout=300)
            return web.json_response({"answer": answer})
        except asyncio.TimeoutError:
            return web.json_response({"answer": "User did not respond in time."})
        finally:
            await prompt.finalize()
            session_manager.pending_approval_messages.pop(approval_key, None)
            session_manager.clear_pending_approval(approval_key)
    except Exception as e:
        return web.json_response({"answer": f"Error: {e}"}, status=500)


async def handle_mcp_send_channel(request):
    try:
        data = await request.json()
        channel_id = data.get("channel_id")
        message = data.get("message", "")

        adapter = get_adapter()
        channel = adapter.resolve_conversation(channel_id)
        if not channel:
            return web.json_response({"error": "Channel not found"}, status=400)

        chunks = [message[i : i + MAX_EMBED_LEN] for i in range(0, len(message), MAX_EMBED_LEN)]
        for chunk in chunks:
            await adapter.send_message(channel, chunk)

        return web.json_response({"answer": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
