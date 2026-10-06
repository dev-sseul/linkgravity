import asyncio
import json
import re
import shlex
import uuid

from aiohttp import web

from api.server import is_tool_allowed
from approval import readonly
from approval.agy_schedule import killed_task_id, to_schedule_args
from approval.mcp_tools import is_linkgravity_tool
from approval.protected_paths import protected_reason
from config import APPROVAL_TIMEOUT_SEC, MAX_EMBED_LEN, logger, session_manager
from messengers.base import ScopeOption
from messengers.registry import get_adapter_for_platform, get_adapter_for_thread
from services import scheduler
from services.schedule_spec import SpecError
from services.schedule_spec import describe as describe_spec
from utils.utils import split_message


async def send_ordered(target_thread_id, send_coro_factory):
    # Shares the answer's stream queue so prompts can't land out of order with the text.
    q = session_manager.get_queue(target_thread_id) if target_thread_id else None
    if not q:
        return await send_coro_factory()

    from config import STREAM_RATE_LIMIT_SEC

    await asyncio.sleep(STREAM_RATE_LIMIT_SEC)

    done = asyncio.get_running_loop().create_future()
    q.put_nowait(("__RUN_ORDERED__", send_coro_factory, done))
    return await done


async def show_tool_call(target_thread_id, text: str, send_coro_factory):
    # Folded into the streaming message as an edit, so each tool call doesn't post (and notify) anew.
    q = session_manager.get_queue(target_thread_id) if target_thread_id else None
    if not q:
        return await send_coro_factory()
    done = asyncio.get_running_loop().create_future()
    q.put_nowait(("__TOOL_CALL__", text, send_coro_factory, done))
    return await done


def build_permission_overrides(tool_name, tool_input):
    # Print mode soft-denies a command without a matching allow rule, even when the hook allows it.
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


async def _send_chunked(adapter, thread, text: str) -> None:
    if not text:
        return
    for part in split_message(text, MAX_EMBED_LEN):
        await adapter.send_message(thread, part)


def _persist_scope_if_granted(prompt_handle):
    # Here rather than in the adapter's button callback: typed or voice replies carry no scope.
    outcome = prompt_handle.outcome
    if outcome and outcome.decision == "allow" and outcome.scope:
        kind, scope = outcome.scope.kind, outcome.scope.scope
        if scope not in session_manager.persistent_allowed[kind]:
            session_manager.persistent_allowed[kind].append(scope)
            session_manager.save_persistent()


async def _adopt_agy_schedule(thread_id: str, args: dict, tool_name: str):
    session = session_manager.get_session(thread_id)
    try:
        job = await scheduler.create(thread_id, session.get("platform", "discord"), args)
    except SpecError as e:
        logger.warning(f"[SCHEDULE] Couldn't adopt agy's {tool_name} call, leaving it to agy: {e}")
        return None
    if job is None:
        return web.json_response({"decision": "deny", "reason": "The user cancelled this schedule in the chat."})
    logger.info(f"[SCHEDULE] Adopted agy's {tool_name} call as #{job['id']}")
    await scheduler.announce(job, "created")
    # Denying is the only way to stop agy's own copy; the reason tells the model the schedule already exists.
    return web.json_response(
        {
            "decision": "deny",
            "reason": (
                f"Done - LinkGravity registered this as schedule #{job['id']} ({describe_spec(job['schedule'])}). "
                "It runs in this chat and survives restarts, so do not create it again. "
                "Manage it with the linkgravity MCP tools schedule_list / schedule_update / schedule_delete."
            ),
        }
    )


def _clean_inline(text: str) -> str:
    return re.sub(r"[*#_`]", "", text).replace("\n", " ").strip()


async def handle_approve_request(request):
    # Tracks approval_keys registered this request so the exception handler can clean them up.
    registered_approval_keys = []
    # Tracks prompts sent this request so a later exception can still finalize (disable) them.
    sent_prompts = []
    try:
        data = await request.json()
        conv_id = data.get("conversation_id")
        tool_name = data.get("tool_name")
        tool_input = data.get("tool_input")
        payload_thread_id = data.get("thread_id")

        logger.debug(f"[APPROVE HOOK] tool_name={tool_name!r} conv_id={conv_id!r} tool_input={tool_input!r}")

        # Ahead of the auto-allow lookup: this must not be overridable by a persistent grant.
        blocked = protected_reason(tool_input)
        if blocked:
            logger.warning(f"Blocked {tool_name} touching protected config: {tool_input!r}")
            return web.json_response({"decision": "deny", "reason": blocked})

        target_thread = None
        target_thread_id = None
        for thread_id_str, sess in session_manager.get_all_sessions().items():
            if sess.get("conversation_id") == conv_id:
                candidate_adapter = get_adapter_for_platform(sess.get("platform", "discord"))
                target_thread = candidate_adapter.resolve_conversation(thread_id_str)
                target_thread_id = thread_id_str
                break

        if not target_thread and payload_thread_id:
            candidate_adapter = get_adapter_for_thread(payload_thread_id)
            resolved_channel = candidate_adapter.resolve_conversation(payload_thread_id)
            if resolved_channel:
                target_thread = resolved_channel
                target_thread_id = payload_thread_id
                if session_manager.get_session(payload_thread_id):
                    session_manager.update_session(payload_thread_id, "conversation_id", conv_id)
                    session_manager.update_session(payload_thread_id, "status", "active")

        if not target_thread:
            for thread_id_str, sess in reversed(list(session_manager.get_all_sessions().items())):
                if sess.get("status") == "pending":
                    candidate_adapter = get_adapter_for_platform(sess.get("platform", "discord"))
                    target_thread = candidate_adapter.resolve_conversation(thread_id_str)
                    session_manager.update_session(thread_id_str, "conversation_id", conv_id)
                    session_manager.update_session(thread_id_str, "status", "active")
                    target_thread_id = thread_id_str
                    break

        def _set_tool_status(key: str):
            if target_thread_id and session_manager.get_session(target_thread_id):
                session_manager.update_session(target_thread_id, key, tool_name)

        adapter = get_adapter_for_thread(target_thread_id) if target_thread_id else get_adapter_for_platform("discord")
        schedule_mode = scheduler.approval_mode(target_thread_id)
        auto_mode = session_manager.is_auto_mode() or schedule_mode == "auto"

        killed = scheduler.find(killed_task_id(tool_name, tool_input))
        if killed:
            scheduler.delete(killed["id"])
            logger.info(f"[SCHEDULE] Adopted agy's manage_task kill as deleting #{killed['id']}")
            await scheduler.announce(killed, "deleted")
            return web.json_response(
                {"decision": "deny", "reason": f"Done - LinkGravity schedule #{killed['id']} is deleted."}
            )

        adopt_args = to_schedule_args(tool_name, tool_input)
        if adopt_args and target_thread_id and session_manager.get_session(target_thread_id):
            adopted = await _adopt_agy_schedule(target_thread_id, adopt_args, tool_name)
            if adopted is not None:
                return adopted

        # Decided up front so neither the global automode nor persistent grants can widen it.
        if schedule_mode == "readonly":
            if readonly.allows(tool_name, tool_input):
                _set_tool_status("current_tool")
                return allow_response(tool_name, tool_input)
            logger.info(f"[SCHEDULE] readonly run denied {tool_name!r}")
            return web.json_response(
                {
                    "decision": "deny",
                    "reason": "This scheduled run is read-only; this action needs the user's approval.",
                }
            )

        if "ask_question" in tool_name:
            _set_tool_status("current_tool")  # no separate approval phase here - it's waiting on the user either way
            if not target_thread:
                return web.json_response({"decision": "deny", "reason": "No target thread found."})
            if schedule_mode == "auto":
                return web.json_response(
                    {
                        "decision": "deny",
                        "reason": "Unattended scheduled run - no one can answer. Use your best judgment.",
                    }
                )

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
            sent_prompts.append(prompt)
            await send_ordered(target_thread_id, lambda: prompt.send(target_thread))

            try:
                chosen_opt = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Question prompt timed out after {APPROVAL_TIMEOUT_SEC}s with no answer (conv_id={conv_id!r})"
                )
                session_manager.clear_pending_approval(approval_key)
                await prompt.finalize()
                return web.json_response({"decision": "deny", "reason": "User did not answer in time."})
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

                is_auto_allowed = auto_mode
                if not is_auto_allowed and "\n" not in sub_cmd and "|" not in sub_cmd:
                    try:
                        tokens = shlex.split(sub_cmd)
                        for scope in session_manager.persistent_allowed.get("commands", []):
                            scope_tokens = shlex.split(scope)
                            if len(scope_tokens) <= len(tokens) and tokens[: len(scope_tokens)] == scope_tokens:
                                is_auto_allowed = True
                                break
                    except ValueError:
                        pass
                elif is_tool_allowed(tool_name, {"CommandLine": sub_cmd}):
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
                sent_prompts.append(prompt)

                sub_cmd_display, sub_cmd_desc, _ = format_bash_display(sub_cmd)
                sub_cmd_formatted = f"```text\n{sub_cmd_display}\n```{sub_cmd_desc}"

                async def _send_bash_prompt(sub_cmd_formatted=sub_cmd_formatted, prompt=prompt):
                    await _send_chunked(adapter, target_thread, sub_cmd_formatted)
                    return await prompt.send(target_thread)

                await send_ordered(target_thread_id, _send_bash_prompt)
                _set_tool_status("pending_approval_tool")

                try:
                    decision = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Tool approval timed out after {APPROVAL_TIMEOUT_SEC}s with no response (conv_id={conv_id!r}, tool={tool_name!r})"
                    )
                    decision = "reject"
                session_manager.clear_pending_approval(approval_key)
                _persist_scope_if_granted(prompt)
                await prompt.finalize()

                if decision == "reject":
                    return web.json_response({"decision": "reject"})

                _set_tool_status("current_tool")

            # A scheduled run can end in NO_REPLY, and echoed tool calls would break that silence.
            if target_thread and tool_msg_text and not prompted and not schedule_mode:
                await show_tool_call(
                    target_thread_id,
                    tool_msg_formatted,
                    lambda: _send_chunked(adapter, target_thread, tool_msg_formatted),
                )

            if not prompted:
                _set_tool_status("current_tool")

            return allow_response(tool_name, tool_input)

        else:
            # The daemon asks the user itself before saving a schedule, so a tool prompt here would be a second one.
            self_confirmed = any(
                is_linkgravity_tool(tool_name, tool_input, t) for t in ("schedule_create", "schedule_list")
            )
            if auto_mode or self_confirmed or is_tool_allowed(tool_name, tool_input):
                if target_thread and tool_msg_text and not schedule_mode:
                    await show_tool_call(
                        target_thread_id,
                        tool_msg_formatted,
                        lambda: _send_chunked(adapter, target_thread, tool_msg_formatted),
                    )
                _set_tool_status("current_tool")
                return allow_response(tool_name, tool_input)
            approval_key = f"{conv_id}:{uuid.uuid4().hex}"
            future = asyncio.get_running_loop().create_future()
            session_manager.set_pending_approval(approval_key, future, conv_id=conv_id)
            registered_approval_keys.append(approval_key)

            scope_options = [ScopeOption(kind="tools", scope=tool_name)]
            prompt = adapter.create_tool_approval_prompt(
                future, "⚠️ Tool Execution Approval Required", tool_msg_formatted, scope_options
            )
            sent_prompts.append(prompt)

            async def _send_prompt():
                await _send_chunked(adapter, target_thread, tool_msg_formatted)
                return await prompt.send(target_thread)

            await send_ordered(target_thread_id, _send_prompt)
            _set_tool_status("pending_approval_tool")

            try:
                decision = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Tool approval timed out after {APPROVAL_TIMEOUT_SEC}s with no response (conv_id={conv_id!r}, tool={tool_name!r})"
                )
                decision = "reject"
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
        for prompt in sent_prompts:
            try:
                await prompt.finalize()
            except Exception:
                pass
        return web.json_response({"decision": "allow"})
