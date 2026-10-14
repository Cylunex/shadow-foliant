from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.scheduled_notification import ScheduledNotificationService
from data.research_store import ResearchStore
from notify.plain_language import compact_notification
from scripts import foliant_scheduled_snapshot as cli


SHANGHAI = ZoneInfo("Asia/Shanghai")
HASH = "a" * 64


def _service(tmp_path, hour, minute):
    now = datetime(2026, 9, 24, hour, minute, tzinfo=SHANGHAI)
    store = ResearchStore(str(tmp_path / "research.db"), connect_fn=sqlite3.connect,
                          is_postgres=False, ensure_schema=True)
    return ScheduledNotificationService(store=store, clock=lambda: now)


def _claim(service, slot, payload_hash=HASH):
    return service.claim(slot=slot, payload_hash=payload_hash, original_lines=24,
                         delivered_lines=8, category="report",
                         version=cli.QQ_SUMMARY_VERSION, actor_id="writer")


def test_all_scheduled_slots_claim_once_and_audit_without_message(tmp_path):
    for hour, minute in ((10, 15), (11, 25), (14, 35), (20, 45)):
        service = _service(tmp_path, hour, minute)
        slot = f"2026-09-24T{hour:02d}:{minute:02d}+08:00"
        assert _claim(service, slot)["should_send"] is True
        assert service.start(slot=slot, payload_hash=HASH, actor_id="writer")["started"] is True
        assert service.start(slot=slot, payload_hash=HASH, actor_id="writer")["started"] is False
        assert service.finish(slot=slot, payload_hash=HASH, actor_id="writer",
                              status="delivered", http_status=200)["recorded"] is True
        replay = _claim(service, slot)
        assert replay["prior_sent"] is True
        assert replay["should_send"] is False
        assert replay["suppression_reason"] == "prior_sent"
    rows = service.audit(actor_id="reader")
    recorded = [row for row in rows if row["delivery_status"] == "delivered"]
    assert len(recorded) == 4
    assert all(row["payload_hash"] == HASH and row["delivered_lines"] == 8
               and row["http_status"] == 200 and row["version"] == cli.QQ_SUMMARY_VERSION
               for row in recorded)
    assert any(row["notification_slot"] == "2026-09-18T20:45+08:00"
               and row["delivery_status"] == "unobserved" for row in rows)
    assert "webhook" not in str(rows)


def test_unattempted_claim_can_resume_but_started_attempt_cannot_replay(tmp_path):
    service = _service(tmp_path, 11, 25)
    slot = "2026-09-24T11:25+08:00"
    assert _claim(service, slot)["should_send"] is True
    # A crash before marking the attempt cannot suppress an unsent slot.
    assert _claim(service, slot)["should_send"] is True
    assert _claim(service, slot, "b" * 64)["should_send"] is False
    assert service.start(slot=slot, payload_hash="b" * 64, actor_id="writer")["started"] is False
    assert service.start(slot=slot, payload_hash=HASH, actor_id="writer")["started"] is True
    pending = _claim(service, slot)
    assert pending["should_send"] is False
    assert pending["suppression_reason"] == "attempt_outcome_unknown"
    assert service.finish(slot=slot, payload_hash=HASH, actor_id="writer",
                          status="unknown", error_code="qq_delivery_unknown")["recorded"] is True
    assert _claim(service, slot)["prior_sent"] is False
    assert _claim(service, slot)["should_send"] is False


def test_summary_keeps_risk_cash_sources_and_close_plan_for_57_holdings():
    actions = [{"symbol": f"600{i:03d}", "name": f"持仓{i}",
                "action": "sell" if i < 11 else "hold"} for i in range(57)]
    snapshot = {
        "trading_day": {"date": "2026-09-24", "confirmed": True},
        "phase": "post_close_review", "status": "degraded",
        "holdings": {"count": 57},
        "trade_plans": {"cash_policy": {"stock_budget": {"status": "degraded"},
                                         "buy_side": {"status": "blocked"}}},
        "holdings_review": {"status": "degraded", "rows": actions,
                            "count": 57, "reviewed_count": 46,
                            "unusable_trade_plan_symbols": ["600011"]},
        "next_session_plan": {"count": 72, "ready_count": 61, "blocked_count": 11,
                              "unusable_trade_plan_symbols": ["600011"]},
        "post_close_review": {"due": True, "conclusion": "盘后闭环部分降级"},
        "formal_selection": {"status": "complete", "formal_top5": [{"symbol": "600001"}]},
        "independent_selection": {"status": "complete", "top5": [{"symbol": "600002"}]},
        "external_independent_research": {"status": "stale", "top5": [{"symbol": "600003"}]},
        "source_comparison": {"pairwise": {"formal_independent": {
            "intersection": [], "formal_only": ["600001"], "independent_only": ["600002"]}}},
    }
    title, body = cli.render_qq_summary(snapshot)
    assert title.startswith("选股与操作｜09-24 ")
    assert len(body.splitlines()) <= 24
    assert len(body) <= 1500
    for expected in ("编号｜名称｜现价｜来源", "编号｜名称｜现价｜操作｜原因",
                     "600001｜", "600002｜", "600010｜持仓10｜现价待核验｜卖出观察"):
        assert expected in body
    assert "旧价位无效" not in body
    assert "正式/独立交集" not in body


def test_slot_window_refuses_historical_backfill(tmp_path):
    service = _service(tmp_path, 14, 35)
    try:
        _claim(service, "2026-09-23T14:35+08:00")
    except ValueError as exc:
        assert str(exc) == "scheduled_notification_slot_outside_window"
    else:
        assert False, "historical slot was accepted"


def test_audit_is_read_scoped_and_slot_mutations_require_writer_capability():
    from webui.access_control import MACHINE_CAPABILITIES, MACHINE_SCOPES

    base = "/api/machine/v1/agent/scheduled-snapshot/notification-"
    assert MACHINE_CAPABILITIES[("GET", base + "audit")] == "foliant.scheduled-report.read"
    assert MACHINE_SCOPES[("GET", base + "audit")] == "stock.portfolio.read"
    state = "/api/machine/v1/agent/scheduled-snapshot/selection-state"
    assert MACHINE_CAPABILITIES[("GET", state)] == "foliant.scheduled-report.read"
    assert MACHINE_SCOPES[("GET", state)] == "stock.portfolio.read"
    for action in ("claim", "start", "finish", "silent"):
        assert MACHINE_CAPABILITIES[("POST", base + action)] == "foliant.selection.preview"
        assert MACHINE_SCOPES[("POST", base + action)] == "stock.research"


def test_protected_routes_wrap_claim_attempt_receipt_and_audit_in_data(tmp_path, monkeypatch):
    from application import scheduled_notification as module
    from webui.scheduled_snapshot_routes import register_scheduled_snapshot_routes

    service = _service(tmp_path, 11, 25)
    monkeypatch.setattr(module, "ScheduledNotificationService", lambda: service)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.agent_identity = SimpleNamespace(agent_id="writer")
        return await call_next(request)

    register_scheduled_snapshot_routes(app, agent_result=lambda value, **_kwargs: value,
                                       agent_error=lambda exc: {"error": str(exc)})
    client = TestClient(app)
    base = "/api/machine/v1/agent/scheduled-snapshot/notification-"
    slot = "2026-09-24T11:25+08:00"
    state = client.get("/api/machine/v1/agent/scheduled-snapshot/selection-state",
                       params={"notification_slot": slot}).json()
    assert state["data"]["notification_slot"] == slot
    assert state["data"]["policy_version"] == "morning-picks-change-only-v1"
    claimed = client.post(base + "claim", json={
        "notification_slot": slot, "payload_hash": HASH,
        "original_lines": 24, "delivered_lines": 8,
        "category": "report", "version": cli.QQ_SUMMARY_VERSION,
    }).json()
    assert claimed["data"]["should_send"] is True
    assert client.post(base + "start", json={"notification_slot": slot,
                                             "payload_hash": HASH}).json()["data"]["started"] is True
    assert client.post(base + "finish", json={"notification_slot": slot,
                                              "payload_hash": HASH,
                                              "status": "delivered", "http_status": 204}).json()["data"]["recorded"] is True
    audit = client.get(base + "audit").json()
    assert audit["data"]["rows"][0]["http_status"] == 204
    assert audit["data"]["rows"][0]["notification_slot"] == slot


def test_pick_claim_http_model_accepts_only_bounded_metadata():
    from pydantic import ValidationError
    from webui.scheduled_snapshot_routes import NotificationClaimReq

    base = {"notification_slot": "2026-10-14T10:15+08:00",
            "payload_hash": HASH, "original_lines": 12,
            "delivered_lines": 12, "category": "report",
            "version": cli.QQ_SUMMARY_VERSION,
            "pick_fingerprint": "b" * 64,
            "pick_origin_fingerprint": "c" * 64,
            "pick_selection_run_id": "formal-1",
            "pick_sources": "正式+独立量化+外部研究",
            "pick_included": True}
    assert NotificationClaimReq.model_validate(base).pick_included is True
    for bad in (base | {"pick_fingerprint": "invalid"},
                base | {"pick_sources": "未知来源"},
                base | {"message_body": "private"}):
        try:
            NotificationClaimReq.model_validate(bad)
        except ValidationError:
            pass
        else:
            assert False, "invalid pick claim metadata accepted"


def test_maximum_daily_review_payload_passes_http_delivery_and_duplicate_suppression(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from application.message_archive import MessageArchiveService
    from notify import notification_router
    from application.daily_review import build_daily_review
    from application import scheduled_notification as module
    from webui.scheduled_snapshot_routes import register_scheduled_snapshot_routes

    service = _service(tmp_path, 20, 45)
    now = datetime(2026, 9, 24, 20, 45, tzinfo=SHANGHAI)
    at = "2026-09-24T14:35:00+08:00"
    holdings = [{"symbol": f"600{i:03d}", "name": f"风险样例{i}", "quantity": 100,
                 "cost_price": 10} for i in range(100)]
    quotes = [{"symbol": row["symbol"], "name": row["name"], "price": 9,
               "as_of": "2026-09-24T15:00:00+08:00", "freshness": "closing_current"}
              for row in holdings]
    previous = {"trade_date": "2026-09-24", "generated_at": at, "holdings": [
        {**row, "quote_as_of": at, "action": "hold", "requested_action": "sell",
         "requested_source": "hard_risk"} for row in holdings]}
    day = {"date": "2026-09-24", "confirmed": True, "is_trading_day": True}
    formal = {"status": "complete", "selection_date": day["date"], "formal_top15": []}
    daily = build_daily_review(now=now, trading_day=day, holdings=holdings, quotes=quotes,
                               formal=formal, previous=previous)
    snapshot = {"schema_version": "scheduled-agent-snapshot-v1", "status": "degraded",
        "trading_day": day, "formal_selection": formal, "trade_plans": {},
        "holdings": {"status": "complete", "count": 100},
        "quotes": {"captured_at": now.isoformat(), "rows": quotes},
        "as_of": {"captured_at": now.isoformat()}, "quality": {"status": "degraded"},
        "daily_review": daily, "holdings_review": {"status": "degraded", "count": 100},
        "next_session_plan": {"status": "missing"}, "post_close_review": {"due": True},
        "notification_policy": {"version": "critical-only-action-required-v1",
            "report_delivery": "scheduled_slot_report", "critical_event_delivery": "intraday_monitor_only",
            "scheduled_delivery": "silent_monitor_owns_critical_events"}}
    payload = cli.qq_payload(snapshot, include_selection=False)
    joined = "\n".join(body for _, body in payload["parts"])
    assert all(joined.count(row["symbol"] + "｜") == 1 for row in holdings)
    assert payload["part_count"] > 1
    assert all(len(body.encode("utf-8")) <= 4096 for _, body in payload["parts"])

    monkeypatch.setattr(module, "ScheduledNotificationService", lambda: service)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.agent_identity = SimpleNamespace(agent_id="writer")
        return await call_next(request)

    register_scheduled_snapshot_routes(app, agent_result=lambda value, **kwargs: value,
                                       agent_error=lambda exc: {"error": str(exc)})
    client = TestClient(app)
    base = "/api/machine/v1/agent/scheduled-snapshot/notification-"
    slot = "2026-09-24T20:45+08:00"
    claim = {"notification_slot": slot, **{key: payload[key] for key in
        ("payload_hash", "original_lines", "delivered_lines", "part_count", "category", "version")}}
    assert client.post(base + "claim", json=claim).json()["data"]["should_send"] is True
    start = {"notification_slot": slot, "payload_hash": payload["payload_hash"]}
    assert client.post(base + "start", json=start).json()["data"]["started"] is True
    archive = MessageArchiveService(store=service.store, key=Fernet.generate_key(), clock=lambda: now)
    monkeypatch.setattr(notification_router, "archive_action", lambda action, data: (
        archive.prepare(**data) if action == "prepare" else
        {"started": archive.start(data["delivery_id"])} if action == "start" else
        {"recorded": archive.finish(**data)}))
    channel_calls = []
    monkeypatch.setitem(notification_router.CHANNELS, "qq", lambda title, body: (
        channel_calls.append((title, body)) or (True, "HTTP 200")))
    monkeypatch.setenv("QQ_WEBHOOK_URL", "https://example.invalid/unused")
    delivered = cli.send_qq(snapshot, payload=payload, notification_slot=slot)
    assert delivered["delivery_status"] == "delivered"
    assert delivered["message_archive_status"] == "recorded"
    channel_body = "\n".join(body for _, body in channel_calls)
    assert all(channel_body.count(row["symbol"] + "｜") == 1 for row in holdings)
    archived = archive.list_messages()
    assert len(archived) == payload["part_count"]
    assert all(archive.detail(row["message_id"])["deliveries"][0]["status"] == "accepted"
               for row in archived)
    finish = {**start, "status": delivered["delivery_status"], "http_status": 200,
        "accepted_part_count": delivered["accepted_part_count"],
        "message_archive_status": delivered["message_archive_status"]}
    assert client.post(base + "finish", json=finish).json()["data"]["recorded"] is True
    assert client.post(base + "claim", json=claim).json()["data"]["should_send"] is False
    audit = client.get(base + "audit").json()["data"]["rows"][0]
    assert audit["accepted_part_count"] == payload["part_count"]
    assert audit["message_archive_status"] == "recorded"
    assert audit["original_lines"] == payload["original_lines"]
    repeat = cli.send_qq(snapshot, payload=payload, notification_slot=slot)
    assert repeat["sent"] is False
    assert len(channel_calls) == payload["part_count"]
