"""Protected machine API for encrypted message archive and channel receipts."""

from __future__ import annotations

import json
from typing import Any, Callable, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field


class StrictArchiveReq(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PrepareReq(StrictArchiveReq):
    message_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source: str = Field(min_length=1, max_length=200)
    source_run_id: str | None = Field(default=None, max_length=200)
    category: str = Field(min_length=1, max_length=60)
    title: str
    original_body: str
    channel: Literal["qq", "email", "dingtalk", "feishu", "wechat_work",
                     "telegram", "discord", "slack", "webhook"]
    final_body: str
    idempotency_key: str | None = None
    business_as_of: str | None = None
    sensitivity: Literal["private", "sensitive"] = "private"
    planned_at: str | None = None
    fallback_from: str | None = None
    retry_of: str | None = None


class StartReq(StrictArchiveReq):
    delivery_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class FinishReq(StartReq):
    status: Literal["accepted", "failed", "unknown"]
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_code: str | None = None
    error_code: str | None = None


def register_message_archive_routes(
    app: FastAPI, *, agent_result: Callable[..., Any],
    agent_error: Callable[..., Any],
) -> None:
    base = "/api/machine/v1/agent/message-archive"

    @app.post(base + "/prepare", operation_id="prepare_agent_message_archive")
    def prepare(req: PrepareReq, request: Request):
        try:
            from application.message_archive import MessageArchiveService
            return agent_result({"status": "complete", "data":
                                 MessageArchiveService().prepare(**req.model_dump())})
        except ValueError as exc:
            if str(exc) in {"message_archive_idempotency_conflict",
                            "message_archive_delivery_conflict"}:
                return JSONResponse({"ok": False, "error": {
                    "code": "message_archive_identity_conflict",
                    "message": "message identity already has different content"}},
                    status_code=409, headers={"Cache-Control": "no-store"})
            return agent_error(exc)
        except Exception as exc:
            return agent_error(exc)

    @app.post(base + "/start", operation_id="start_agent_message_archive_delivery")
    def start(req: StartReq, request: Request):
        try:
            from application.message_archive import MessageArchiveService
            return agent_result({"status": "complete", "data": {
                "started": MessageArchiveService().start(req.delivery_id)}})
        except Exception as exc:
            return agent_error(exc)

    @app.post(base + "/finish", operation_id="finish_agent_message_archive_delivery")
    def finish(req: FinishReq, request: Request):
        try:
            from application.message_archive import MessageArchiveService
            return agent_result({"status": "complete", "data": {
                "recorded": MessageArchiveService().finish(
                    req.delivery_id, status=req.status, http_status=req.http_status,
                    provider_code=req.provider_code, error_code=req.error_code)}})
        except Exception as exc:
            return agent_error(exc)

    @app.get(base, operation_id="list_agent_message_archive")
    def list_messages(request: Request, from_date: str | None = None,
                      to_date: str | None = None,
                      offset: int = Query(0, ge=0),
                      limit: int = Query(20, ge=1, le=50)):
        try:
            from application.message_archive import MessageArchiveService
            service = MessageArchiveService()
            rows = service.list_messages(
                from_date=from_date, to_date=to_date, offset=offset, limit=limit)
            has_more = bool(service.list_messages(
                from_date=from_date, to_date=to_date,
                offset=max(0, offset) + len(rows), limit=1))
            response = agent_result({"status": "complete", "data": {
                "rows": rows, "offset": offset, "limit": min(limit, 50),
                "has_more": has_more}})
            response.headers["Cache-Control"] = "no-store"
            return response
        except Exception as exc:
            return agent_error(exc)

    @app.get(base + "/export", operation_id="export_agent_message_archive")
    def export(request: Request, from_date: str, to_date: str,
               offset: int = Query(0, ge=0),
               limit: int = Query(20, ge=1, le=50)):
        try:
            from application.message_archive import MessageArchiveService
            service = MessageArchiveService()
            rows = service.list_messages(from_date=from_date, to_date=to_date,
                                         offset=offset, limit=limit)
            details = [service.detail(row["message_id"]) for row in rows]
            has_more = bool(service.list_messages(
                from_date=from_date, to_date=to_date,
                offset=max(0, offset) + len(rows), limit=1))
            body = "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                           for item in details if item is not None)
            return StreamingResponse(iter((body.encode("utf-8"),)),
                                     media_type="application/x-ndjson",
                                     headers={"Cache-Control": "no-store",
                                              "Content-Disposition": "attachment; filename=message-archive.ndjson",
                                              "X-Archive-Page-Count": str(len(details)),
                                              "X-Archive-Has-More": str(has_more).lower()})
        except Exception as exc:
            return agent_error(exc)

    @app.get(base + "/{message_id}", operation_id="get_agent_message_archive")
    def detail(message_id: str, request: Request):
        try:
            from application.message_archive import MessageArchiveService
            row = MessageArchiveService().detail(message_id)
            if row is None:
                return JSONResponse({"status": "missing", "data": None},
                                    status_code=404, headers={"Cache-Control": "no-store"})
            return JSONResponse({"status": "complete", "data": row},
                                headers={"Cache-Control": "no-store"})
        except Exception as exc:
            return agent_error(exc)
