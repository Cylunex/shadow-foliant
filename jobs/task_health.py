"""Task health is separate from optional features and historical run evidence."""
from __future__ import annotations

from datetime import datetime
import json
import re
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
FAILED_STATES = {"error", "failed", "timeout", "interrupted", "degraded", "partial"}

_MODEL_NUMBERS = ("requested", "observed", "settled", "waiting_books")


def format_eod_models(result: dict) -> str:
    recovered = result.get("recovered_original_sessions") or {}
    metrics = {
        "requested": result.get("requested_count"),
        "observed": result.get("execution_fact_count"),
        "settled": sum((result.get("settled") or {}).values()) + sum(
            sum(day.get("cohorts_settled", {}).values()) for day in recovered.values()),
        "waiting_books": result.get("waiting_books"),
        "oldest_pending": result.get("oldest_pending_date"),
        "pending_reason": result.get("pending_reason"),
    }
    books = result.get("model_books") or {}
    blockers = {book: books[book]["blocker"] for book in ("top5", "source:formal")
                if books.get(book, {}).get("blocker")}
    if blockers:
        metrics["book_blockers"] = json.dumps(blockers, separators=(",", ":"))
    return " ".join([f"decision_loop={result.get('status', 'missing')}",
                     *[f"model_{key}={value}" for key, value in metrics.items() if value is not None]])


def eod_model_metrics(detail: str) -> dict:
    result = {}
    status = re.search(r"\bdecision_loop=([a-zA-Z0-9_-]+)", detail)
    if status:
        result["decision_loop_status"] = status.group(1)[:40]
    elif "decision_loop_err=" in detail:
        result["decision_loop_status"] = "error"
    for name in _MODEL_NUMBERS:
        match = re.search(rf"\bmodel_{name}=(\d+)", detail)
        if match:
            result[f"model_{name}"] = int(match.group(1))
    for name, pattern in (("oldest_pending", r"\d{4}-\d{2}-\d{2}"),
                          ("pending_reason", r"[a-z0-9_]+")):
        match = re.search(rf"\bmodel_{name}=({pattern})", detail)
        if match:
            result[f"model_{name}"] = match.group(1)[:80]
    blockers = re.search(r"\bmodel_book_blockers=(\S+)", detail)
    if blockers:
        try:
            result["model_book_blockers"] = json.loads(blockers.group(1))
        except ValueError:
            pass
    return result


def scheduled_outcome(run: dict) -> dict:
    if run.get("job_name") != "eod_outcomes":
        return run
    detail = run.get("error") or ""
    metrics = eod_model_metrics(detail)
    status = run["status"]
    if status == "success" and (
        metrics.get("decision_loop_status", "complete") not in {"complete", "success"}
        or any(marker in detail for marker in ("rec_err=", "signal_err=", "external_err="))
    ):
        status = "partial"
    return {**run, "status": status, "metrics": metrics}


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
