"""Terminable child-process boundary for mutable scheduled jobs."""

from __future__ import annotations

import multiprocessing
import os
import pickle
import queue
import sys
import time
from typing import Any, Callable


def _child_entry(name: str, func: Callable[..., Any], args: tuple[Any, ...],
                 kwargs: dict[str, Any], cancel_event, result_queue) -> None:
    module = sys.modules.get(getattr(func, "__module__", ""))
    cancel_map = getattr(module, "_TASK_CANCEL_EVENTS", None) if module else None
    waiting_map = getattr(module, "_TASK_WAITING_ON", None) if module else None
    if isinstance(cancel_map, dict):
        cancel_map[name] = cancel_event
    try:
        func(*args, **kwargs)
        result_queue.put({
            "status": "complete",
            "waiting_on": (
                str(waiting_map.get(name)) if isinstance(waiting_map, dict)
                and waiting_map.get(name) else None
            ),
        })
    except BaseException as exc:  # child must report a bounded category, not provider text
        result_queue.put({
            "status": "error",
            "error_category": type(exc).__name__[:80],
        })
    finally:
        if isinstance(cancel_map, dict):
            cancel_map.pop(name, None)


def is_spawn_safe(func: Callable[..., Any], args: tuple[Any, ...],
                  kwargs: dict[str, Any]) -> bool:
    if "<locals>" in str(getattr(func, "__qualname__", "")):
        return False
    try:
        pickle.dumps((func, args, kwargs))
        return True
    except Exception:
        return False


def run_isolated_task(name: str, func: Callable[..., Any], args: tuple[Any, ...],
                      kwargs: dict[str, Any], *, timeout_seconds: int,
                      cancel_grace_seconds: int = 15) -> dict[str, Any]:
    """Run one picklable task and guarantee no child survives the returned timeout result."""
    if not is_spawn_safe(func, args, kwargs):
        allow_inline = str(os.getenv("FOLIANT_ALLOW_INLINE_TASKS") or "").lower() in {
            "1", "true", "yes"
        }
        if not allow_inline:
            return {
                "status": "error",
                "error_category": "task_not_spawn_safe",
                "elapsed": 0.0,
                "isolation": "rejected",
            }
        # Tests and explicitly enabled legacy callbacks retain an observable compatibility path.
        started = time.monotonic()
        try:
            func(*args, **kwargs)
            return {"status": "complete", "elapsed": time.monotonic() - started,
                    "isolation": "compatibility-inline"}
        except BaseException as exc:
            return {"status": "error", "error_category": type(exc).__name__[:80],
                    "elapsed": time.monotonic() - started,
                    "isolation": "compatibility-inline"}

    context = multiprocessing.get_context("spawn")
    cancel_event = context.Event()
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_child_entry,
        args=(str(name), func, args, kwargs, cancel_event, result_queue),
        name=f"foliant-job-{str(name)[:32]}",
    )
    process.daemon = False
    started = time.monotonic()
    process.start()
    process.join(timeout=max(0.01, float(timeout_seconds)))
    exceeded_deadline = process.is_alive()
    if process.is_alive():
        cancel_event.set()
        process.join(timeout=max(0.01, float(cancel_grace_seconds)))
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=5)
        if process.is_alive():
            result_queue.close()
            return {"status": "timeout", "elapsed": time.monotonic() - started,
                    "terminated": False, "isolation": "spawn"}
    try:
        result = result_queue.get(timeout=1)
    except queue.Empty:
        result = {
            "status": "error",
            "error_category": "worker_process_exited",
        }
    finally:
        result_queue.close()
    result["elapsed"] = time.monotonic() - started
    result["isolation"] = "spawn"
    result["terminated"] = not process.is_alive()
    if exceeded_deadline:
        result["child_status"] = result.get("status")
        result["status"] = "deadline_exceeded" if result.get("waiting_on") else "timeout"
    return result
