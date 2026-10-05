import asyncio
import secrets
import time
from datetime import datetime, timedelta
from typing import NamedTuple

from config import APPROVAL_TIMEOUT_SEC, DATA_DIR, logger, session_manager
from core import platform_health
from core.atomic_io import atomic_write_json, safe_load_json
from messengers.registry import get_adapter_for_platform, registered_platforms
from services import schedule_spec as spec

STORE_FILE = DATA_DIR / "schedules.json"
# Covers a daemon restart or `lgy update`; anything later than this is treated as missed and skipped.
GRACE = timedelta(minutes=10)
TICK_SEC = 30
APPROVAL_HELP = {
    "readonly": "read-only tools & commands only",
    "ask": "show approval buttons and wait",
    "auto": "allow everything",
}
APPROVAL_MODES = tuple(APPROVAL_HELP)
CANCEL = "cancel - don't save"
THIS_CHAT = "This chat"
ELSEWHERE = "Somewhere else"
KIND_LABELS = {"channel": "Channel", "dm": "DM"}
NO_REPLY = "NO_REPLY"


class Pick(NamedTuple):
    cancelled: bool
    # None means "no override": the creating chat as destination, or the chat's current model.
    value: dict | str | None = None


CANCELLED_PICK = Pick(True)


class TurnMode(NamedTuple):
    approval: str
    task: asyncio.Task


_jobs: dict[str, dict] = safe_load_json(STORE_FILE, {}, logger=logger)
_running: set[str] = set()
_turn_modes: dict[str, TurnMode] = {}
_wake = asyncio.Event()


def _save() -> None:
    atomic_write_json(STORE_FILE, _jobs)


def approval_mode(thread_id: str | None) -> str | None:
    entry = _turn_modes.get(thread_id or "")
    # A user message replaces the scheduled turn's handler task; from then on normal approvals apply.
    if entry and session_manager.get_handler_task(thread_id) is entry.task:
        return entry.approval
    return None


async def _ask(job: dict, question: str, options: list[str], select: bool = False) -> int | None:
    # Asked in the chat the schedule is being made from; the model never gets to answer these.
    from api.ui_routes import send_ordered

    adapter, ref = _resolve(job)
    if ref is None:
        raise spec.SpecError("That chat isn't reachable right now.")
    session = session_manager.get_session(job["thread_id"]) or {}
    future = asyncio.get_running_loop().create_future()
    key = f"schedule:{secrets.token_hex(6)}"
    session_manager.set_pending_approval(
        key, future, "ask_question", conv_id=session.get("conversation_id"), thread_id=job["thread_id"]
    )
    if select:
        prompt = adapter.create_select_prompt(future, question, options)
    else:
        prompt = adapter.create_question_prompt(future, question, options, allow_write_in=False)
    try:
        await send_ordered(job["thread_id"], lambda: prompt.send(ref))
        answer = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        answer = None
    finally:
        session_manager.clear_pending_approval(key)
        await prompt.finalize()
    if isinstance(answer, int):
        index = answer
    elif answer in options:
        index = options.index(answer)
    else:
        # A typed reply instead of a click: match it against each option's first word.
        typed = str(answer or "").strip().lower()
        heads = [opt.lower().split()[0] for opt in options]
        index = heads.index(typed) if typed in heads else None
    return None if index is None or options[index] == CANCEL else index


async def _choose_target(job: dict) -> Pick:
    head = f"'{job['name']}' ({spec.describe(job['schedule'])})"
    choice = await _ask(job, f"Where should {head} post its results?", [THIS_CHAT, ELSEWHERE, CANCEL])
    if choice is None:
        return CANCELLED_PICK
    if choice == 0:
        return Pick(False)

    platforms = [p for p in registered_platforms() if platform_health.get_status(p) == "running"]
    if len(platforms) > 1:
        choice = await _ask(job, "Which messenger?", [p.capitalize() for p in platforms] + [CANCEL])
        if choice is None:
            return CANCELLED_PICK
        platform = platforms[choice]
    elif platforms:
        platform = platforms[0]
    else:
        await _notify(job, "⚠️ No messenger is connected right now.")
        return CANCELLED_PICK
    try:
        destinations = await get_adapter_for_platform(platform).list_destinations()
    except Exception as e:
        logger.exception(f"[SCHEDULE] Listing {platform} destinations failed: {e}")
        destinations = []
    if not destinations:
        await _notify(job, f"⚠️ There's no {platform.capitalize()} chat LinkGravity is allowed to post to.")
        return CANCELLED_PICK

    kinds = sorted({d.kind for d in destinations})
    if len(kinds) > 1:
        labels = [KIND_LABELS[k] for k in kinds]
        choice = await _ask(job, f"{platform.capitalize()}: a DM or a channel?", labels + [CANCEL])
        if choice is None:
            return CANCELLED_PICK
        destinations = [d for d in destinations if d.kind == kinds[choice]]
    servers = sorted({d.server for d in destinations if d.server})
    if len(servers) > 1:
        choice = await _ask(job, "Which server?", servers + [CANCEL], select=True)
        if choice is None:
            return CANCELLED_PICK
        destinations = [d for d in destinations if d.server == servers[choice]]
    if len(destinations) > 1:
        choice = await _ask(job, "Which chat?", [d.label for d in destinations] + [CANCEL], select=True)
        if choice is None:
            return CANCELLED_PICK
        destinations = [destinations[choice]]

    chosen = destinations[0]
    if (chosen.platform, chosen.conversation_id) == (job["platform"], job["thread_id"]):
        return Pick(False)
    where = f"{chosen.server} {chosen.label}" if chosen.server else chosen.label
    return Pick(
        False,
        {
            "platform": chosen.platform,
            "conversation_id": chosen.conversation_id,
            "label": f"{platform.capitalize()} · {where}",
        },
    )


def _session_model(job: dict) -> str:
    from config import bot_settings
    from utils.utils import get_current_model

    session = session_manager.get_session(job["thread_id"]) or {}
    # Same precedence as the reply footer.
    return session.get("model") or bot_settings.get("default_model") or get_current_model()


async def _choose_model(job: dict) -> Pick:
    from cogs.general_cog import cached_models

    current = _session_model(job)
    models = [m for m in cached_models if m != current]
    choice = await _ask(job, "Which model should run it?", [f"Same as this chat ({current})", *models, CANCEL], True)
    if choice is None:
        return CANCELLED_PICK
    return Pick(False, None if choice == 0 else models[choice - 1])


async def _choose_approval(job: dict, verb: str) -> str | None:
    target = f" → {job['target']['label']}" if job.get("target") else ""
    model = f" · {job['model']}" if job.get("model") else ""
    question = (
        f"{verb} schedule '{job['name']}' ({spec.describe(job['schedule'])}){target}{model}. "
        "When it runs while you're away, how should approvals be handled?"
    )
    options = [f"{mode} - {help_text}" for mode, help_text in APPROVAL_HELP.items()] + [CANCEL]
    choice = await _ask(job, question, options)
    return None if choice is None else APPROVAL_MODES[choice]


def prepare(thread_id: str, platform: str, args: dict) -> dict:
    # Validation only, so bad input is reported before the user is asked anything.
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        raise spec.SpecError("prompt is required.")
    now = spec.now()
    schedule = spec.build(args, now)
    job_id = secrets.token_hex(3)
    while job_id in _jobs:
        job_id = secrets.token_hex(3)
    return {
        "id": job_id,
        "name": (args.get("name") or prompt[:40]).strip(),
        "prompt": prompt,
        "schedule": schedule,
        "approval": None,
        "target": None,
        "model": None,
        "platform": platform,
        "thread_id": thread_id,
        "enabled": True,
        "created_at": now.isoformat(),
        "next_run": spec.next_after(schedule, now).isoformat(),
        "last_run": None,
        "last_status": None,
    }


async def confirm_and_save(job: dict) -> dict | None:
    for key, choose in (("target", _choose_target), ("model", _choose_model)):
        pick = await choose(job)
        if pick.cancelled:
            return None
        job[key] = pick.value
    job["approval"] = await _choose_approval(job, "New")
    if job["approval"] is None:
        return None
    _jobs[job["id"]] = job
    _save()
    _wake.set()
    return job


async def create(thread_id: str, platform: str, args: dict) -> dict | None:
    return await confirm_and_save(prepare(thread_id, platform, args))


def _new_schedule(job: dict, args: dict, now: datetime) -> dict:
    schedule = job["schedule"]
    if any(args.get(k) for k in ("at", "delay", "every", "cron")):
        return spec.build(args, now)
    if args.get("start") and schedule["kind"] == "every":
        return spec.build({"every": schedule["every"], "start": args["start"]}, now)
    return schedule


async def update(job_id: str, args: dict) -> dict | None:
    job = _get(job_id)
    now = spec.now()
    schedule = _new_schedule(job, args, now)
    # Everything is asked before anything changes, so a cancel leaves the schedule exactly as it was.
    draft = {**job, "schedule": schedule}
    for flag, key, choose in (
        ("change_destination", "target", _choose_target),
        ("change_model", "model", _choose_model),
    ):
        if args.get(flag):
            pick = await choose(draft)
            if pick.cancelled:
                return None
            draft[key] = pick.value
    if args.get("approval"):
        draft["approval"] = await _choose_approval(draft, "Change approval for")
        if draft["approval"] is None:
            return None
    job.update(schedule=schedule, target=draft.get("target"), model=draft.get("model"), approval=draft["approval"])
    if args.get("prompt"):
        job["prompt"] = args["prompt"].strip()
    if args.get("name"):
        job["name"] = args["name"].strip()
    if args.get("enabled") is not None:
        job["enabled"] = bool(args["enabled"])
    nxt = spec.next_after(job["schedule"], now)
    job["next_run"] = nxt.isoformat() if nxt else None
    _save()
    _wake.set()
    return job


def delete(job_id: str) -> dict:
    job = _jobs.pop(_get(job_id)["id"])
    _save()
    return job


def run_now(job_id: str) -> dict:
    job = _get(job_id)
    if job["id"] in _running:
        raise spec.SpecError("That schedule is already running.")
    asyncio.create_task(_fire(job, spec.now(), advance=False))
    return job


def list_jobs() -> list[dict]:
    return sorted(_jobs.values(), key=lambda j: j.get("next_run") or "9999")


def find(job_id: str) -> dict | None:
    return _jobs.get((job_id or "").strip().lstrip("#"))


def _get(job_id: str) -> dict:
    job = _jobs.get((job_id or "").strip().lstrip("#"))
    if not job:
        raise spec.SpecError(f"No schedule with id {job_id!r}.")
    return job


def describe(job: dict, current_thread: str | None = None) -> str:
    nxt = datetime.fromisoformat(job["next_run"]).strftime("%Y-%m-%d %H:%M") if job.get("next_run") else "-"
    state = "" if job.get("enabled", True) else " (paused)"
    here = " · this chat" if current_thread and job["thread_id"] == current_thread else ""
    target = f" → {job['target']['label']}" if job.get("target") else ""
    model = f" · model: {job['model']}" if job.get("model") else ""
    last = f" · last: {job['last_status']}" if job.get("last_status") else ""
    return (
        f"`#{job['id']}` **{job['name']}**{state} - {spec.describe(job['schedule'])}{target} · next {nxt}"
        f"{model} · approval: {job['approval']}{here}{last}"
    )


def _resolve(job: dict):
    if platform_health.get_status(job["platform"]) != "running":
        return None, None
    try:
        adapter = get_adapter_for_platform(job["platform"])
    except RuntimeError:
        return None, None
    return adapter, adapter.resolve_conversation(job["thread_id"])


def _advance(job: dict, now: datetime) -> None:
    nxt = spec.next_after(job["schedule"], now)
    if nxt is None:
        _jobs.pop(job["id"], None)
    else:
        job["next_run"] = nxt.isoformat()
    _save()


async def announce(job: dict, verb: str) -> None:
    # Posted by the daemon itself so the chat always sees what was scheduled, whatever the model reports.
    await _notify(job, f"🗓️ Schedule {verb}: {describe(job)}")


async def _notify(job: dict, text: str) -> None:
    adapter, ref = _resolve(job)
    if ref is None:
        logger.warning(f"[SCHEDULE] Couldn't reach chat for #{job['id']}: {text}")
        return
    try:
        await adapter.send_message(ref, text)
    except Exception as e:
        logger.warning(f"[SCHEDULE] Notify failed for #{job['id']}: {e}")


async def _wait_until_idle(thread_id: str) -> None:
    while (task := session_manager.get_handler_task(thread_id)) and not task.done():
        await asyncio.sleep(5)


def _is_silent(text: str) -> bool:
    from core.agy_runner import EMPTY_RESPONSE

    # agy's output includes its narration before tool calls, and "say nothing" prompts often end empty.
    tail = text.strip().rstrip("`'\".").rstrip()
    return text.strip() == EMPTY_RESPONSE or not tail or tail.endswith(NO_REPLY)


async def _drain(thread_id: str, queue: asyncio.Queue) -> None:
    # Not streamed, since posting depends on the full answer; the queue keeps approvals ordered and interruptible.
    from services.streaming import _clear_current_tool

    while True:
        item = await queue.get()
        if isinstance(item, tuple) and item and item[0] == "__RUN_ORDERED__":
            _, factory, done = item
            try:
                result = await factory()
                if not done.done():
                    done.set_result(result)
            except Exception as e:
                if not done.done():
                    done.set_exception(e)
            continue
        chunk = item[0] if isinstance(item, tuple) else item
        if chunk == "__END__":
            if session_manager.get_queue(thread_id) is queue:
                session_manager.remove_queue(thread_id)
            _clear_current_tool(thread_id)
            return
        if str(chunk).startswith("__CONV_ID__:"):
            session_manager.update_session(thread_id, "conversation_id", chunk.split(":", 1)[1])


async def _run_turn(job: dict, due: datetime, session: dict, adapter, ref) -> str:
    from services.response import send_agy_response
    from utils.utils import agy_new_conversation, agy_send_message

    thread_id = job["thread_id"]
    content = (
        f"[Scheduled task '{job['name']}' (#{job['id']}) fired at {due:%Y-%m-%d %H:%M}; "
        f"the user may not be watching. If this run turns up nothing worth telling the user, "
        f"reply with exactly {NO_REPLY} and nothing else.]\n\n{job['prompt']}"
    )
    queue = asyncio.Queue()
    session_manager.register_queue(thread_id, queue)
    drain = asyncio.create_task(_drain(thread_id, queue))
    start_time = time.time()
    try:
        conv_id = session.get("conversation_id")
        kwargs = {
            "model": job.get("model") or session.get("model"),
            "stream_queue": queue,
            "thread_id": thread_id,
            "cwd": session.get("cwd"),
        }
        if conv_id:
            text = await agy_send_message(conv_id, content, **kwargs)
        else:
            text, conv_id = await agy_new_conversation(content, **kwargs)
            session.update(conversation_id=conv_id, created_at=datetime.now().isoformat(), status="active")
            session_manager.save_sessions()
    finally:
        await queue.put(("__END__", True))
        await drain

    if _is_silent(text):
        logger.info(f"[SCHEDULE] #{job['id']} had nothing to report")
        return "ok (silent)"
    header = f"⏰ **{job['name']}** (`#{job['id']}`)"
    # The reply footer names the model, so it has to see the schedule's own choice.
    session = {**session, "model": kwargs["model"]}
    target = job.get("target")
    if target:
        try:
            target_adapter = get_adapter_for_platform(target["platform"])
            target_ref = target_adapter.resolve_conversation(target["conversation_id"])
        except RuntimeError:
            target_ref = None
        if target_ref is not None:
            await target_adapter.send_message(target_ref, header)
            await send_agy_response(
                target_ref, text, {**session, "platform": target["platform"]}, None, start_time, conv_id
            )
            return "ok"
        await adapter.send_message(ref, f"⚠️ Couldn't reach {target['label']} - posting here instead.")
    await adapter.send_message(ref, header)
    await send_agy_response(ref, text, session, None, start_time, conv_id)
    return "ok"


async def _fire(job: dict, due: datetime, advance: bool = True) -> None:
    from handlers.thread_reply import _run_tracked

    job_id, thread_id = job["id"], job["thread_id"]
    _running.add(job_id)
    if advance:
        _advance(job, max(due, spec.now()))
    status = "ok"
    try:
        await _wait_until_idle(thread_id)
        adapter, ref = _resolve(job)
        session = session_manager.get_session(thread_id)
        if ref is None or not session:
            status = "error: chat or session not found"
            logger.warning(f"[SCHEDULE] #{job_id} skipped - {status}")
            return
        if not session.get("conversation_id") and session.get("status") != "pending":
            status = "error: session has no conversation"
            await adapter.send_message(ref, f"⚠️ Scheduled run `#{job_id}` skipped - this session has no conversation.")
            return

        async def _tracked_turn():
            nonlocal status
            _turn_modes[thread_id] = TurnMode(job["approval"], asyncio.current_task())
            try:
                status = await _run_turn(job, due, session, adapter, ref)
            finally:
                if (entry := _turn_modes.get(thread_id)) and entry.task is asyncio.current_task():
                    _turn_modes.pop(thread_id, None)

        await _run_tracked(thread_id, _tracked_turn())
    except asyncio.CancelledError:
        status = "stopped"
    except Exception as e:
        status = f"error: {e}"
        logger.exception(f"[SCHEDULE] #{job_id} failed: {e}")
    finally:
        _running.discard(job_id)
        if job_id in _jobs:
            job["last_run"] = due.isoformat()
            job["last_status"] = status
            _save()


async def _tick() -> None:
    now = spec.now()
    for job in list(_jobs.values()):
        if not job.get("enabled", True) or not job.get("next_run"):
            continue
        due = datetime.fromisoformat(job["next_run"])
        if due > now:
            continue
        if job["id"] in _running:
            logger.info(f"[SCHEDULE] #{job['id']} still running - skipping the {due:%H:%M} run")
            _advance(job, now)
            continue
        if now - due > GRACE:
            logger.warning(f"[SCHEDULE] #{job['id']} missed its {due:%Y-%m-%d %H:%M} run - skipping")
            _advance(job, now)
            await _notify(
                job, f"⏭️ Missed scheduled run `#{job['id']}` **{job['name']}** (due {due:%m-%d %H:%M}) - skipped."
            )
            continue
        if _resolve(job)[1] is None:
            continue  # platform still connecting; retried next tick while inside GRACE
        asyncio.create_task(_fire(job, due))


async def run_loop() -> None:
    logger.info(f"[SCHEDULE] Scheduler started with {len(_jobs)} job(s)")
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.exception(f"[SCHEDULE] Tick failed: {e}")
        _wake.clear()
        # Short polls against the wall clock, so suspend/resume or clock changes can't make a long sleep overshoot.
        try:
            await asyncio.wait_for(_wake.wait(), timeout=TICK_SEC)
        except asyncio.TimeoutError:
            pass


CHAT_USAGE = "Usage: `/schedules` · `/schedules delete|pause|resume|run <id>`"


async def chat_command(text: str, thread_id: str | None) -> str:
    words = (text or "").split()
    if not words or words[0] == "list":
        jobs = list_jobs()
        if not jobs:
            return "No schedules yet. Ask the agent to schedule something."
        return "\n".join(["🗓️ **Schedules**", *(f"• {describe(j, thread_id)}" for j in jobs), "", CHAT_USAGE])
    if len(words) != 2:
        return CHAT_USAGE
    verb, job_id = words
    try:
        if verb == "delete":
            return f"🗑️ Deleted: {describe(delete(job_id))}"
        if verb in ("pause", "resume"):
            return f"🗓️ Updated: {describe(await update(job_id, {'enabled': verb == 'resume'}))}"
        if verb == "run":
            return f"▶️ Running now: {describe(run_now(job_id))}"
    except spec.SpecError as e:
        return f"⚠️ {e}"
    return CHAT_USAGE
