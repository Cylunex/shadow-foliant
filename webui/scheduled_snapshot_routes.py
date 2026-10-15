"""Private scheduled-report Agent HTTP route."""

from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from application.scheduled_report_contract import (REPORT_MAX_DELIVERED_LINES,
    REPORT_MAX_ORIGINAL_LINES, REPORT_MAX_PARTS)


class NotificationClaimReq(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    notification_slot: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T(?:10:15|11:25|14:35|20:45)\+08:00$")
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_lines: int = Field(ge=1, le=REPORT_MAX_ORIGINAL_LINES)
    delivered_lines: int = Field(ge=1, le=REPORT_MAX_DELIVERED_LINES)
    part_count: int = Field(default=1, ge=1, le=REPORT_MAX_PARTS)
    category: Literal["report"]
    version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    pick_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pick_origin_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pick_selection_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    pick_research_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    pick_sources: str = Field(default="", pattern=r"^(?:|正式\+独立量化(?:\+外部研究)?)$")
    pick_included: bool = False


class NotificationStartReq(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    notification_slot: str
    payload_hash: str


class NotificationSilentReq(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    notification_slot: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T(?:11:25|14:35|20:45)\+08:00$")


class NotificationFinishReq(NotificationStartReq):
    status: Literal["delivered", "failed", "partial", "unknown"]
    http_status: int | None = None
    error_code: str | None = None
    accepted_part_count: int = Field(default=0, ge=0, le=REPORT_MAX_PARTS)
    message_archive_status: str | None = Field(default=None, pattern=r"^(recorded|partial|unknown|unrecorded|suppressed)$")
    message_archive_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    market_context: dict[str, Any] | None = None
    account_context: dict[str, Any] | None = None


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
                # Keep the declared Agent transport budget. The response helper
                # losslessly uses gzip when the full post-close contract is
                # larger than the inline wire budget.
                max_bytes=262144,
                request=request,
            )
        except Exception as exc:
            return agent_error(exc)

    @app.get("/api/machine/v1/agent/scheduled-snapshot/notification-audit",
             operation_id="get_agent_scheduled_notification_audit")
    def notification_audit(request: Request):
        try:
            from application.scheduled_notification import ScheduledNotificationService
            return agent_result({"status": "complete", "data": {
                "rows": ScheduledNotificationService().audit(
                    actor_id=str(request.state.agent_identity.agent_id))}})
        except Exception as exc:
            return agent_error(exc)

    @app.get("/api/machine/v1/agent/scheduled-snapshot/selection-state",
             operation_id="get_agent_scheduled_selection_state")
    def selection_state(notification_slot: str, request: Request):
        try:
            from application.scheduled_notification import ScheduledNotificationService
            return agent_result({"status": "complete", "data":
                ScheduledNotificationService().selection_state(
                    slot=notification_slot,
                    actor_id=str(request.state.agent_identity.agent_id))})
        except Exception as exc:
            return agent_error(exc)

    @app.post("/api/machine/v1/agent/scheduled-snapshot/notification-silent",
              operation_id="record_agent_scheduled_notification_silent")
    def notification_silent(req: NotificationSilentReq, request: Request):
        try:
            from application.scheduled_notification import ScheduledNotificationService
            return agent_result({"status": "complete", "data":
                ScheduledNotificationService().record_silent(
                    slot=req.notification_slot,
                    actor_id=str(request.state.agent_identity.agent_id))})
        except Exception as exc:
            return agent_error(exc)

    @app.post("/api/machine/v1/agent/scheduled-snapshot/notification-claim",
              operation_id="claim_agent_scheduled_notification")
    def notification_claim(req: NotificationClaimReq, request: Request):
        try:
            from application.scheduled_notification import ScheduledNotificationService
            return agent_result({"status": "complete", "data": ScheduledNotificationService().claim(
                slot=req.notification_slot, payload_hash=req.payload_hash,
                original_lines=req.original_lines, delivered_lines=req.delivered_lines,
                category=req.category, version=req.version, part_count=req.part_count,
                pick_fingerprint=req.pick_fingerprint,
                pick_origin_fingerprint=req.pick_origin_fingerprint,
                pick_selection_run_id=req.pick_selection_run_id,
                pick_research_id=req.pick_research_id,
                pick_sources=req.pick_sources, pick_included=req.pick_included,
                actor_id=str(request.state.agent_identity.agent_id))})
        except Exception as exc:
            return agent_error(exc)

    @app.post("/api/machine/v1/agent/scheduled-snapshot/notification-start",
              operation_id="start_agent_scheduled_notification")
    def notification_start(req: NotificationStartReq, request: Request):
        try:
            from application.scheduled_notification import ScheduledNotificationService
            return agent_result({"status": "complete", "data": ScheduledNotificationService().start(
                slot=req.notification_slot, payload_hash=req.payload_hash,
                actor_id=str(request.state.agent_identity.agent_id))})
        except Exception as exc:
            return agent_error(exc)

    @app.post("/api/machine/v1/agent/scheduled-snapshot/notification-finish",
              operation_id="finish_agent_scheduled_notification")
    def notification_finish(req: NotificationFinishReq, request: Request):
        try:
            from application.scheduled_notification import ScheduledNotificationService
            return agent_result({"status": "complete", "data": ScheduledNotificationService().finish(
                slot=req.notification_slot, payload_hash=req.payload_hash,
                actor_id=str(request.state.agent_identity.agent_id), status=req.status,
                http_status=req.http_status, error_code=req.error_code,
                accepted_part_count=req.accepted_part_count,
                message_archive_status=req.message_archive_status,
                message_archive_id=req.message_archive_id,
                market_context=req.market_context,
                account_context=req.account_context)})
        except Exception as exc:
            return agent_error(exc)
