import asyncio
import functools
import os
import re
import signal

from config import logger

active_processes = {}
agy_start_lock = asyncio.Lock()
# Threads killed intentionally - agy's SIGTERM exit code isn't reliable enough to tell otherwise.
_intentionally_stopped = set()

# Forced stdout buffer size (see _find_libstdbuf) - big enough for one write, small enough not to delay polling.
_STDOUT_BUFFER_SIZE = 65536


@functools.lru_cache(maxsize=1)
def _find_libstdbuf() -> str | None:
    """Find libstdbuf.so, the shared library `stdbuf` LD_PRELOADs. Linux-only
    (no macOS/Windows equivalent implemented) - returns None there too.

    agy runs commands under a PTY, so glibc line-buffers instead of
    fully-buffering stdout - agy's completion-detection misreads the gap
    between line writes as "done," truncating multi-line output to its
    first line despite exit code 0. LD_PRELOADing this forces full
    buffering instead. Returns None (no fix applied) if not found.
    """
    candidates = [
        "/usr/lib/x86_64-linux-gnu/coreutils/libstdbuf.so",  # Debian/Ubuntu
        "/usr/lib/aarch64-linux-gnu/coreutils/libstdbuf.so",
        "/usr/libexec/coreutils/libstdbuf.so",  # Fedora/RHEL
        "/usr/lib/coreutils/libstdbuf.so",  # Arch
        "/usr/lib/libstdbuf.so",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    # lru_cache: only runs once per process.
    try:
        import subprocess

        result = subprocess.run(
            ["find", "/usr", "-name", "libstdbuf.so"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        found = [line for line in result.stdout.strip().splitlines() if line]
        if found:
            return found[0]
    except Exception:
        pass

    logger.warning(
        "[AGY ENV] libstdbuf.so not found - run_command output for "
        "multi-line commands may come back truncated. Add its path to "
        "_find_libstdbuf()'s candidates list if coreutils is installed "
        "somewhere nonstandard."
    )
    return None


def stop_active_process(thread_id: str) -> bool:
    """Kill the agy subprocess for this thread, if any. Also used when a
    new voice utterance interrupts a still-in-flight turn. Returns
    whether a process was actually found and signaled.
    """
    target_proc = active_processes.get(thread_id)
    if not target_proc:
        return False
    _intentionally_stopped.add(thread_id)
    if os.name == "nt":
        try:
            target_proc.send_signal(signal.CTRL_BREAK_EVENT)
        except Exception:
            target_proc.kill()
    else:
        try:
            os.killpg(os.getpgid(target_proc.pid), signal.SIGTERM)
        except Exception:
            target_proc.kill()
    return True


async def _get_latest_conversation_id() -> str:
    try:
        from pathlib import Path

        history_dir = Path.home() / ".gemini/antigravity-cli/brain"
        if not history_dir.exists():
            return ""
        dirs = [d for d in history_dir.iterdir() if d.is_dir()]
        if not dirs:
            return ""
        latest = max(dirs, key=lambda x: x.stat().st_mtime)
        return latest.name
    except Exception:
        return ""


async def run_agy(
    *args, timeout: int = 300, stream_queue: asyncio.Queue = None, thread_id: str = None, cwd: str = None
) -> str:
    args_list = list(args)

    if cwd:
        expanded_cwd = os.path.expanduser(cwd)
        os.makedirs(expanded_cwd, exist_ok=True)
        cwd_param = expanded_cwd
        args_list = ["--add-dir", expanded_cwd] + args_list
    else:
        cwd_param = None

    try:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                env = os.environ.copy()
                env["AGY_DISCORD_BOT"] = "1"
                env["PYTHONUNBUFFERED"] = "1"
                if thread_id:
                    env["DISCORD_THREAD_ID"] = thread_id

                libstdbuf_path = _find_libstdbuf()  # works around agy's output-truncation bug
                if libstdbuf_path:
                    existing_preload = env.get("LD_PRELOAD", "")
                    env["LD_PRELOAD"] = (
                        f"{libstdbuf_path}:{existing_preload}" if existing_preload else libstdbuf_path
                    )
                    env["_STDBUF_O"] = str(_STDOUT_BUFFER_SIZE)

                kwargs = {
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                    "stdin": asyncio.subprocess.DEVNULL,
                    "cwd": cwd_param,
                    "env": env,
                }

                if os.name == "nt":
                    import subprocess

                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                elif hasattr(os, "setsid"):
                    kwargs["preexec_fn"] = os.setsid

                from config import AGY_BIN

                cmd = [AGY_BIN] + args_list

                async with agy_start_lock:
                    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
                    if thread_id:
                        active_processes[thread_id] = proc

                    if stream_queue and "--conversation" not in args:
                        await asyncio.sleep(1.0)
                        latest_conv_id = await _get_latest_conversation_id()
                        if latest_conv_id:
                            await stream_queue.put(("__CONV_ID__:" + latest_conv_id, False))

                stdout_chunks = []
                stderr_chunks = []

                import codecs

                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

                ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

                async def read_stdout():
                    while True:
                        chunk = await proc.stdout.read(1024)
                        if not chunk:
                            decoded = decoder.decode(b"", final=True)
                            if decoded:
                                clean = ansi_escape.sub("", decoded)
                                stdout_chunks.append(clean)
                                if stream_queue is not None:
                                    await stream_queue.put((clean, False))
                            break
                        decoded = decoder.decode(chunk, final=False)
                        if decoded:
                            clean = ansi_escape.sub("", decoded)
                            stdout_chunks.append(clean)
                            if stream_queue is not None:
                                await stream_queue.put((clean, False))
                                if (
                                    "(Calls tool:" in clean
                                    or "Tool Output:" in clean
                                    or "Tool Execute:" in clean
                                    or clean.startswith("● ")
                                ):
                                    await stream_queue.put(("__SPLIT__", True))

                async def read_stderr():
                    while True:
                        chunk = await proc.stderr.read(1024)
                        if not chunk:
                            break
                        stderr_chunks.append(chunk)

                async def _gather_pipes():
                    await asyncio.gather(read_stdout(), read_stderr())

                gather_task = asyncio.create_task(_gather_pipes())
                wait_task = asyncio.create_task(proc.wait())

                # Slices let the timeout pause during a pending Discord approval (up to 3600s).
                from config import session_manager as _sm

                poll_slice = 5.0
                remaining_budget = float(timeout)
                timed_out = False
                while True:
                    done, _pending_tasks = await asyncio.wait(
                        [gather_task, wait_task], return_when=asyncio.FIRST_COMPLETED, timeout=poll_slice
                    )
                    if done:
                        break
                    if not _sm.pending_approvals:
                        remaining_budget -= poll_slice
                        if remaining_budget <= 0:
                            timed_out = True
                            break

                if timed_out:
                    gather_task.cancel()
                    raise asyncio.TimeoutError()

                if wait_task in done:
                    try:
                        await asyncio.wait_for(gather_task, timeout=1.0)
                    except asyncio.TimeoutError:
                        gather_task.cancel()
                elif gather_task in done:
                    try:
                        await asyncio.wait_for(wait_task, timeout=2.0)
                    except asyncio.TimeoutError:
                        pass

                text = "".join(stdout_chunks).strip()
                err_text = b"".join(stderr_chunks).decode(errors="replace").strip()
                if err_text:
                    logger.warning(f"[AGY STDERR] {err_text}")

                if proc.returncode is not None and proc.returncode != 0:
                    if thread_id in _intentionally_stopped or proc.returncode in (-15, -9, 15, 9, 143, 137):
                        error_msg = "🛑 Generation stopped by user request."
                        if stream_queue is not None:
                            await stream_queue.put(("\n\n" + error_msg, True))
                        return error_msg

                    if "authentication failed or timed out" in text or "authentication failed or timed out" in err_text:
                        if attempt < max_retries - 1:
                            logger.warning("[AGY RETRY] Authentication timeout. Retrying...")
                            await asyncio.sleep(2.0)
                            continue

                    error_msg = f"⚠️ Agent exited abnormally (exit code {proc.returncode})\n"
                    if err_text:
                        error_msg += f"```\n{err_text}\n```\n"
                    if text:
                        error_msg += f"Output:\n```\n{text}\n```\n"
                    logger.error(f"[AGY ERROR] {error_msg}")
                    if stream_queue is not None:
                        await stream_queue.put(("\n\n" + error_msg, True))
                    return error_msg

                # DEBUG-only (LOG_LEVEL) raw stdout capture.
                logger.debug(f"[AGY RAW STDOUT] {text!r}")
                return text or "(Empty response)"

            except asyncio.CancelledError:
                try:
                    if proc:
                        if os.name == "nt":
                            proc.send_signal(signal.CTRL_BREAK_EVENT)
                        else:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass
                msg = "🛑 AI Task manually stopped by user."
                if stream_queue is not None:
                    await stream_queue.put(("\n\n" + msg, True))
                return msg
            except asyncio.TimeoutError:
                try:
                    if proc:
                        if os.name == "nt":
                            proc.send_signal(signal.CTRL_BREAK_EVENT)
                        else:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass
                if attempt < max_retries - 1:
                    logger.warning("[AGY RETRY] Global timeout. Retrying...")
                    await asyncio.sleep(2.0)
                    continue
                msg = "🛑 AI Task timed out."
                if stream_queue is not None:
                    await stream_queue.put(("\n\n" + msg, True))
                return msg
            except Exception as e:
                logger.exception(f"[AGY UNEXPECTED ERROR] {e}")
                if attempt < max_retries - 1:
                    logger.warning(f"[AGY RETRY] Unexpected error: {e}. Retrying...")
                    await asyncio.sleep(2.0)
                    continue
                msg = f"🛑 AI Task encountered an error: {str(e)}"
                if stream_queue is not None:
                    await stream_queue.put(("\n\n" + msg, True))
                return msg
    finally:
        if thread_id and thread_id in active_processes:
            del active_processes[thread_id]
        _intentionally_stopped.discard(thread_id)


async def agy_new_conversation(
    content: str, model: str = None, stream_queue: asyncio.Queue = None, thread_id: str = None, cwd: str = None
) -> tuple[str, str]:
    # --print consumes the next token as the prompt, so the flag must come first.
    args = ["--dangerously-skip-permissions", "--print", content]
    if model:
        args.extend(["--model", model])
    result_text = await run_agy(*args, stream_queue=stream_queue, thread_id=thread_id, cwd=cwd)
    conv_id = await _get_latest_conversation_id()
    return result_text, conv_id


async def agy_send_message(
    conv_id: str,
    content: str,
    model: str = None,
    stream_queue: asyncio.Queue = None,
    thread_id: str = None,
    cwd: str = None,
) -> str:
    args = ["--dangerously-skip-permissions", "--print", content, "--conversation", conv_id]
    if model:
        args.extend(["--model", model])
    return await run_agy(*args, stream_queue=stream_queue, thread_id=thread_id, cwd=cwd)


def get_current_model() -> str:
    import json
    from pathlib import Path

    try:
        settings_path = Path.home() / ".gemini/antigravity-cli/settings.json"
        if settings_path.exists():
            data = json.loads(settings_path.read_text())
            return data.get("model", "Default")
    except Exception:
        logger.warning("Failed to read current model from settings.json")
    return "Default"


async def generate_thread_title(user_input: str, response: str) -> str:
    fallback = user_input.replace("\n", " ").strip()
    if len(fallback) > 50:
        fallback = fallback[:47] + "..."

    try:
        prompt = (
            "Summarize the topic of this exchange in 5 words or fewer, as a short title. "
            "Reply with ONLY the title text, no quotes, no punctuation at the end.\n\n"
            f"Question: {user_input[:500]}\n\nAnswer: {response[:500]}"
        )
        title = await run_agy("--print", prompt, timeout=30)
        title = title.strip().strip('"').strip("'")
        if not title or len(title) > 80:
            return fallback
        return title
    except Exception as e:
        logger.warning(f"AI thread-title generation failed, falling back to raw input: {e}")
        return fallback
