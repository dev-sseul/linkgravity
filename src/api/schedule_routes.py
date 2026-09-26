from aiohttp import web

from api.attach_routes import _resolve_target
from config import logger
from services import scheduler
from services.schedule_spec import SpecError
from services.schedule_spec import describe as describe_spec

CANCELLED = "The user cancelled this in the chat. Don't retry unless they ask again."


def _public(job: dict, thread_id: str | None = None) -> dict:
    keys = ("id", "name", "prompt", "approval", "enabled", "next_run", "last_run", "last_status")
    return {k: job[k] for k in keys} | {
        "schedule": describe_spec(job["schedule"]),
        "this_chat": job["thread_id"] == thread_id,
        "posts_to": (job.get("target") or {}).get("label", "the chat it was created in"),
    }


async def handle_schedule(request):
    try:
        data = await request.json()
        action = data.get("action")
        target = _resolve_target(str(data.get("thread_id") or ""))
        thread_id = target[0] if target else None

        if action == "list":
            jobs = [_public(job, thread_id) for job in scheduler.list_jobs()]
            return web.json_response({"ok": True, "jobs": jobs})

        if action == "create":
            if not target:
                return web.json_response({"ok": False, "error": "Could not tell which chat this schedule belongs to."})
            job = await scheduler.create(thread_id, target[1].get("platform", "discord"), data)
            if job is None:
                return web.json_response({"ok": False, "error": CANCELLED})
            await scheduler.announce(job, "created")
            return web.json_response({"ok": True, "job": _public(job, thread_id)})

        job_id = data.get("id") or ""
        if action == "update":
            job = await scheduler.update(job_id, data)
            if job is None:
                return web.json_response({"ok": False, "error": CANCELLED})
            await scheduler.announce(job, "updated")
        elif action == "delete":
            job = scheduler.delete(job_id)
            await scheduler.announce(job, "deleted")
        elif action == "run_now":
            job = scheduler.run_now(job_id)
        else:
            return web.json_response({"ok": False, "error": f"Unknown action {action!r}."})
        return web.json_response({"ok": True, "job": _public(job, thread_id)})
    except SpecError as e:
        return web.json_response({"ok": False, "error": str(e)})
    except Exception as e:
        logger.exception(f"Error in handle_schedule: {e}")
        return web.json_response({"ok": False, "error": str(e)})
