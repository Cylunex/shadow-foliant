"""Task health is separate from optional features and historical run evidence."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
FAILED_STATES = {"error", "failed", "timeout", "interrupted", "degraded", "partial"}


def run_timestamp(run: dict | None) -> float:
    if not run:
        return float("-inf")
    value = run.get("finished_at") or run.get("started_at") or run.get("requested_at")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return float("-inf")


def effective_task_run(task: dict) -> dict | None:
    manual, scheduled = task.get("manual_run"), task.get("last_run")
    if manual and manual.get("status") in {"queued", "running"}:
        return manual
    if manual and run_timestamp(manual) > run_timestamp(scheduled):
        return manual
    return scheduled or manual


def summarize_task_health(tasks: list[dict], *, recent_limit: int = 5) -> dict:
    failed, disabled_core, paused = [], [], []
    for task in tasks:
        if not task.get("enabled"):
            paused.append(task["name"])
            if task.get("core"):
                disabled_core.append(task["name"])
            # Keep old failures in the catalog, not in current service health.
            continue
        last = effective_task_run(task)
        if last and last.get("status") in FAILED_STATES:
            failed.append({"name": task["name"], "status": last["status"],
                           "error": last.get("error"),
                           "at": last.get("finished_at") or last.get("started_at")})
    failed.sort(key=lambda row: run_timestamp({"finished_at": row["at"]}), reverse=True)
    return {"total": len(tasks), "failed_recent": failed[:recent_limit],
            "failed_count": len(failed), "disabled_core": disabled_core,
            "paused": paused,
            "running_manual": [task["manual_run"] for task in tasks
                               if (task.get("manual_run") or {}).get("status")
                               in {"queued", "running"}]}
