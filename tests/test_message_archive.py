from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from application.message_archive import MessageArchiveService
from data.research_store import ResearchStore
from notify import notification_router


def service(tmp_path):
    path = str(tmp_path / "archive.db")
    store = ResearchStore(path, connect_fn=sqlite3.connect,
                          is_postgres=False, ensure_schema=True)
    return MessageArchiveService(store=store, key=Fernet.generate_key(),
                                 clock=lambda: datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)), path


def prepare(svc, *, channel="qq", message_id=None, original="完整原稿：卖出 600001", final="摘要：卖出 600001",
            key="slot:20260924:1435"):
    return svc.prepare(message_id=message_id or uuid4().hex, source="test.source",
                       source_run_id="run-1", category="report", title="计划",
                       original_body=original, channel=channel, final_body=final,
                       idempotency_key=key, business_as_of="2026-09-24T14:35+08:00")


def test_encrypted_message_multi_channel_partial_and_export(tmp_path):
    svc, path = service(tmp_path)
    qq = prepare(svc)
    email = prepare(svc, channel="email", final="邮件全文")
    assert qq["message_id"] == email["message_id"]
    assert svc.start(qq["delivery_id"]) is True
    assert svc.start(qq["delivery_id"]) is False
    assert svc.finish(qq["delivery_id"], status="accepted", http_status=204) is True
    assert svc.start(email["delivery_id"]) is True
    assert svc.finish(email["delivery_id"], status="failed", error_code="provider_rejected") is True
    detail = svc.detail(qq["message_id"])
    assert detail["status"] == "partial"
    assert detail["original_body"] == "完整原稿：卖出 600001"
    assert {d["channel"]: d["status"] for d in detail["deliveries"]} == {
        "qq": "accepted", "email": "failed"}
    assert len(svc.list_messages(from_date="2026-09-24", to_date="2026-09-24")) == 1
    assert len(svc.list_messages()) == 1
    with open(path, "rb") as handle:
        raw = handle.read()
    assert "完整原稿".encode() not in raw
    assert "邮件全文".encode() not in raw


def test_same_slot_cannot_be_resent_or_changed(tmp_path):
    svc, _ = service(tmp_path)
    first = prepare(svc)
    assert svc.start(first["delivery_id"])
    replay = prepare(svc)
    assert replay["should_send"] is False
    assert replay["suppression_reason"] == "attempt_outcome_unknown"
    assert svc.detail(first["message_id"])["deliveries"][0]["suppressed_count"] == 1
    with pytest.raises(ValueError, match="idempotency_conflict"):
        prepare(svc, original="changed")
    with pytest.raises(ValueError, match="delivery_conflict"):
        prepare(svc, final="changed")


def test_router_archives_original_and_each_submitted_body_without_sending(tmp_path, monkeypatch):
    svc, _ = service(tmp_path)
    calls = []
    monkeypatch.setattr(notification_router, "archive_action", lambda action, body: (
        svc.prepare(**body) if action == "prepare" else
        {"started": svc.start(body["delivery_id"])} if action == "start" else
        {"recorded": svc.finish(**body)}))
    monkeypatch.setitem(notification_router.CHANNELS, "qq", lambda title, body: (
        calls.append(("qq", body)) or (False, "HTTP 500")))
    monkeypatch.setitem(notification_router.CHANNELS, "email", lambda title, body: (
        calls.append(("email", body)) or (True, "ok")))
    result = notification_router.send("report", "计划", "短摘要", only_channels=["qq"],
                                      fallback="email", original_body="完整原稿",
                                      source="test.router", compact=False)
    assert calls == [("qq", "短摘要"), ("email", "短摘要")]
    detail = svc.detail(result.message_id)
    assert detail["original_body"] == "完整原稿"
    assert {d["channel"]: d["status"] for d in detail["deliveries"]} == {
        "qq": "failed", "email": "accepted"}
    assert next(d for d in detail["deliveries"] if d["channel"] == "email")["fallback_from"] == "qq"


def test_archive_unavailable_still_sends_alert(monkeypatch):
    monkeypatch.setattr(notification_router, "archive_action", lambda *_: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setitem(notification_router.CHANNELS, "qq", lambda title, body: (True, "HTTP 200"))
    result = notification_router.send("alert", "告警", "正文", only_channels=["qq"])
    assert result["qq"][0] is True
    assert result.archive_status == "unrecorded"


def test_direct_http_receipt_is_recorded_without_destination(tmp_path, monkeypatch):
    from notify import archive_gateway
    svc, path = service(tmp_path)
    monkeypatch.setattr(archive_gateway, "archive_action", lambda action, body: (
        svc.prepare(**body) if action == "prepare" else
        {"started": svc.start(body["delivery_id"])} if action == "start" else
        {"recorded": svc.finish(**body)}))
    assert archive_gateway.archived_call(
        channel="feishu", title="告警", original_body="全部原文",
        final_body='{"content":"提交正文"}',
        sender=lambda: {"ok": False, "http_status": 200,
                        "provider_code": "rejected", "error_code": "provider_rejected"},
        source="test.direct", category="alert") is False
    row = svc.list_messages()[0]
    delivery = svc.detail(row["message_id"])["deliveries"][0]
    assert (delivery["status"], delivery["http_status"], delivery["provider_code"]) == (
        "failed", 200, "rejected")
    assert "https://" not in open(path, "rb").read().decode("latin-1")


def test_analysis_email_and_webhook_share_one_logical_message(tmp_path, monkeypatch):
    from notify import archive_gateway
    from notify.notification_service import NotificationService
    svc, _ = service(tmp_path)
    monkeypatch.setattr(archive_gateway, "archive_action", lambda action, body: (
        svc.prepare(**body) if action == "prepare" else
        {"started": svc.start(body["delivery_id"])} if action == "start" else
        {"recorded": svc.finish(**body)}))
    service_obj = NotificationService.__new__(NotificationService)
    service_obj.config = {'email_enabled': True, 'webhook_enabled': True,
                          'webhook_url': 'https://private.invalid/hook',
                          'webhook_type': 'feishu', 'webhook_keyword': '通知'}
    monkeypatch.setattr(service_obj, '_send_email_unarchived', lambda *_: True)
    monkeypatch.setattr(service_obj, '_send_webhook_unarchived', lambda *_: {
        'ok': True, 'http_status': 200, 'provider_code': 'accepted', 'error_code': None})
    assert service_obj.send_analysis_result('分析', '原稿') is True
    rows = svc.list_messages()
    assert len(rows) == 1
    detail = svc.detail(rows[0]['message_id'])
    assert detail['status'] == 'accepted'
    assert {d['channel'] for d in detail['deliveries']} == {'email', 'feishu'}


def test_protected_route_page_detail_and_export_are_read_only(tmp_path, monkeypatch):
    from application import message_archive as archive_module
    from webui.message_archive_routes import register_message_archive_routes

    svc, _ = service(tmp_path)
    row = prepare(svc)
    monkeypatch.setattr(archive_module, "MessageArchiveService", lambda: svc)
    app = FastAPI()
    register_message_archive_routes(
        app, agent_result=lambda data: JSONResponse(data),
        agent_error=lambda exc: JSONResponse({"error": type(exc).__name__}, status_code=400))
    client = TestClient(app)
    page = client.get("/api/machine/v1/agent/message-archive",
                      params={"from_date": "2026-09-24", "to_date": "2026-09-24"})
    assert page.status_code == 200
    assert page.json()["data"]["rows"][0]["message_id"] == row["message_id"]
    assert "original_body" not in page.text
    detail = client.get("/api/machine/v1/agent/message-archive/" + row["message_id"])
    assert detail.json()["data"]["original_body"] == "完整原稿：卖出 600001"
    assert detail.headers["cache-control"] == "no-store"
    export = client.get("/api/machine/v1/agent/message-archive/export",
                        params={"from_date": "2026-09-24", "to_date": "2026-09-24"})
    assert export.status_code == 200
    assert "完整原稿" in export.text
    assert export.headers["cache-control"] == "no-store"
    assert svc.detail(row["message_id"])["deliveries"][0]["status"] == "pending"
