"""Durable preview Run worker owned by the jobs-hub process."""

from __future__ import annotations

import logging
import multiprocessing
import os
import socket
import threading
import time
import uuid
from typing import Any

from application.results import stable_failure
from application.run_repository import RunRepository

_log = logging.getLogger("foliant.run_worker")

_EVENT_TYPES = {
    "security-research": "foliant.research-report.ready",
    "selection": "foliant.selection.completed",
    "backtest": "foliant.backtest.completed",
}


def execute_run_payload(run: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a persisted canonical request without relying on a closure from the Web process."""
    run_id = str(run["run_id"])
    payload = dict(run.get("request_payload") or {})
    kind = str(run.get("run_kind") or "")
    if kind == "security-research":
        from application.services import SecurityResearchService

        return SecurityResearchService._analyze_preview(
            run_id, str(payload["symbol"]), str(payload.get("depth") or "quick")
        )
    if kind == "selection":
        from application.services import SelectionRunService
        from data.research_store import ResearchStore

        return SelectionRunService(store=ResearchStore(ensure_schema=False))._selection_preview(
            run_id,
            str(payload["selection_date"]),
            str(payload.get("decision_mode") or "preopen"),
        )
    if kind == "backtest":
        from application.services import BacktestRunService

        return BacktestRunService._backtest_preview(run_id, payload)
    raise ValueError("unsupported_run_kind")


def _execute_claimed(run: dict[str, Any], worker_id: str) -> None:
    repository = RunRepository(ensure_schema=False)
    run_id = str(run["run_id"])
    try:
        result = execute_run_payload(run)
        repository.complete(
            run_id,
            result,
            event_type=_EVENT_TYPES[str(run["run_kind"])],
            worker_id=worker_id,
        )
    except Exception as exc:  # noqa: BLE001 - every claimed Run must converge
        repository.fail(run_id, stable_failure(exc), worker_id=worker_id)


class FoliantRunWorker:
    """Claim with a DB lease and execute each Run in a terminable child process."""

    def __init__(self, repository: RunRepository | None = None, *, poll_seconds: float = 2.0,
                 lease_seconds: int = 120) -> None:
        self.repository = repository or RunRepository(ensure_schema=False)
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.lease_seconds = max(30, int(lease_seconds))
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: multiprocessing.Process | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name="foliant-run-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._process and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=10)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)

    def run_once(self) -> bool:
        run = self.repository.claim_next(self.worker_id, lease_seconds=self.lease_seconds)
        if not run:
            return False
        run_id = str(run["run_id"])
        timeout_seconds = max(30, int(run.get("timeout_seconds") or 1800))
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_execute_claimed,
            args=(run, self.worker_id),
            name=f"foliant-run-{run_id[:10]}",
        )
        process.daemon = True
        self._process = process
        process.start()
        started = time.monotonic()
        heartbeat_every = max(5.0, self.lease_seconds / 3)
        next_heartbeat = started + heartbeat_every
        while process.is_alive() and not self._stop.wait(0.5):
            now = time.monotonic()
            if now - started >= timeout_seconds:
                process.terminate()
                process.join(timeout=10)
                self.repository.fail(run_id, "execution_timeout", worker_id=self.worker_id)
                break
            if now >= next_heartbeat:
                if not self.repository.heartbeat(
                    run_id, self.worker_id, lease_seconds=self.lease_seconds
                ):
                    process.terminate()
                    process.join(timeout=10)
                    self.repository.finalize_cancel(run_id, self.worker_id)
                    break
                next_heartbeat = now + heartbeat_every
        if process.is_alive():
            process.terminate()
        process.join(timeout=10)
        self._process = None
        current = self.repository.get(run_id) or {}
        if current.get("status") == "running":
            if current.get("cancel_requested"):
                self.repository.finalize_cancel(run_id, self.worker_id)
            else:
                self.repository.fail(run_id, "worker_process_exited", worker_id=self.worker_id)
        return True

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(self.poll_seconds)
            except Exception:  # noqa: BLE001 - supervisor-facing worker must keep polling
                _log.exception("durable Run worker iteration failed")
                self._stop.wait(min(30.0, self.poll_seconds * 2))
