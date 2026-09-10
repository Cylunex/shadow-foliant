"""Private scheduled-report Agent HTTP route."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Request


def register_scheduled_snapshot_routes(
    app: FastAPI,
    *,
    agent_result: Callable[..., Any],
    agent_error: Callable[..., Any],
) -> None:
    @app.get(
        "/api/machine/v1/agent/scheduled-snapshot",
        operation_id="get_agent_scheduled_snapshot",
    )
    def agent_scheduled_snapshot(request: Request):
        try:
            from application.scheduled_snapshot import ScheduledSnapshotService

            identity = request.state.agent_identity
            return agent_result(
                ScheduledSnapshotService().read(owner_id=str(identity.agent_id)),
                max_bytes=262144,
            )
        except Exception as exc:
            return agent_error(exc)
