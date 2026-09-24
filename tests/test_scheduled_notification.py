from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3
from zoneinfo import ZoneInfo

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
    assert len(rows) == 4
    assert all(row["payload_hash"] == HASH and row["delivered_lines"] == 8
               and row["http_status"] == 200 and row["version"] == cli.QQ_SUMMARY_VERSION
               for row in rows)
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
    assert title == "ShadowFoliant 计划摘要"
    assert len(body.splitlines()) == 8
    assert len(body) <= 900
    assert compact_notification("report", body) == body
    for expected in ("11只", "持仓57只", "旧价位无效", "买入侧关闭", "旧排名不采用",
                     "正式/独立交集0只", "46/57", "61/72", "缺口11", "未覆盖全部持仓"):
        assert expected in body
    assert "600010" not in body


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
    for action in ("claim", "start", "finish"):
        assert MACHINE_CAPABILITIES[("POST", base + action)] == "foliant.selection.preview"
        assert MACHINE_SCOPES[("POST", base + action)] == "stock.research"
