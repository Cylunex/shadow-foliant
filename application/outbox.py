"""Runtime-neutral background publisher for metadata-only domain events."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from application.run_repository import RunRepository

_log = logging.getLogger("foliant.outbox")


class OutboxPublisher:
    """Deliver persisted events through an injected adapter without owning a bus SDK."""

    def __init__(self, repository: RunRepository,
                 publish: Callable[[str, dict[str, Any]], None], *,
                 interval_seconds: float = 5.0) -> None:
        self.repository = repository
        self.publish = publish
        self.interval_seconds = max(0.5, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def publish_once(self, *, limit: int = 50) -> int:
        published = 0
        for event in self.repository.pending_outbox(limit=limit):
            try:
                self.publish(event["event_type"], event["payload"])
            except Exception as exc:  # noqa: BLE001 - adapter failures remain retryable
                self.repository.record_outbox_failure(event["event_id"])
                _log.warning("domain_event_publish_failed category=%s", type(exc).__name__)
                continue
            if self.repository.mark_outbox_published(event["event_id"]):
                published += 1
        return published

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="foliant-outbox", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(0.0, timeout))

    def _run(self) -> None:
        while not self._stop.is_set():
            self.publish_once()
            self._stop.wait(self.interval_seconds)
