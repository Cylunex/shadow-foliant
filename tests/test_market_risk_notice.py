from datetime import datetime, timedelta
import sqlite3
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from application.market_risk_notice import (
    INDEX_CODES, VERSION, accepted, account_risk, current_indices, market_lines,
    newer_reference, report_reference, transition,
)
from scripts.foliant_scheduled_snapshot import qq_payload
from scripts.scheduled_qq_format import render
from application.message_archive import MessageArchiveService
from data.research_store import ResearchStore
from notify import notification_router
from jobs import intraday_decision_monitor as monitor
from application.scheduled_notification import ScheduledNotificationService
from webui.scheduled_snapshot_routes import NotificationClaimReq, NotificationFinishReq


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 15, 10, 15, 20, tzinfo=TZ)


def market(now=NOW, changes=(-1.6, -2.0, -2.8)):
    names = list(INDEX_CODES)[:3]
    raw = {INDEX_CODES[name][2:]: {"price": 3500, "change_pct": pct,
                                  "quote_time": now.strftime("%Y%m%d%H%M%S")}
           for name, pct in zip(names, changes)}
    return current_indices(now, lambda _codes: raw)


def test_provider_time_and_single_index_language():
    value = market(changes=(-1.6,))
    assert value["status"] == "current"
    assert "上证指数明显下跌" in market_lines(value)[1]
    assert "普跌" not in "".join(market_lines(value))
    assert current_indices(NOW + timedelta(minutes=9),
                           lambda _codes: {"000001": {"price": 3500,
                               "change_pct": -2, "quote_time": NOW.strftime("%Y%m%d%H%M%S")}}
                           )["rows"] == []
    assert current_indices(NOW, lambda _codes: {"000001": {
        "price": 3500, "change_pct": -2, "retrieved_at": NOW.isoformat()}})["rows"] == []


def test_account_estimate_requires_full_same_batch_quotes_and_asset_classes():
    snapshot = {"trading_day": {"date": "2026-10-15"},
                "as_of": {"captured_at": NOW.isoformat()},
                "holdings": {"status": "complete", "count": 2, "rows": [
                    {"symbol": "600001", "name": "股票", "quantity": 100,
                     "asset_class": "stock"},
                    {"symbol": "510001", "name": "ETF", "quantity": 200,
                     "asset_class": "fund_or_etf_or_lof"}]},
                "quotes": {"rows": [
                    {"symbol": "600001", "price": 9, "last_close": 10,
                     "change_pct": -10, "freshness": "actionable", "as_of": NOW.isoformat()},
                    {"symbol": "510001", "price": 1, "last_close": 1.1,
                     "change_pct": -9.09, "freshness": "actionable", "as_of": NOW.isoformat()}]}}
    assert account_risk(snapshot)["day_change_estimate_cny"] == -120
    snapshot["holdings"]["rows"][1]["asset_class"] = "unknown"
    assert account_risk(snapshot)["day_change_estimate_cny"] is None
    snapshot["holdings"]["rows"][1]["asset_class"] = "fund_or_etf_or_lof"
    snapshot["quotes"]["rows"][1]["as_of"] = "2026-10-14T15:00:00+08:00"
    assert account_risk(snapshot)["day_change_estimate_cny"] is None


def test_new_drop_worsening_cooldown_and_recovery_hysteresis():
    normal = market(changes=(-0.1, -0.2, -0.3))
    state, event = transition(normal, {}, NOW)
    assert event is None
    drop = market(NOW + timedelta(minutes=20))
    state, event = transition(drop, state, NOW + timedelta(minutes=20))
    assert event and event["level"] >= 1
    state = accepted(state, event, NOW + timedelta(minutes=20))
    same, event = transition(drop, state, NOW + timedelta(minutes=21))
    assert event is None
    deeper = market(NOW + timedelta(minutes=25), (-2.7, -2.0, -2.8))
    same, event = transition(deeper, same, NOW + timedelta(minutes=25))
    assert event is None  # cooldown
    later = market(NOW + timedelta(minutes=55), (-2.7, -2.0, -2.8))
    same, event = transition(later, same, NOW + timedelta(minutes=55))
    assert event and event["episode"] == 2
    state = accepted(same, event, NOW + timedelta(minutes=55))
    for minute in (75, 95):
        state, event = transition(market(NOW + timedelta(minutes=minute),
                                         (-0.2, -0.3, -0.4)), state,
                                  NOW + timedelta(minutes=minute))
        assert event is None
    assert state["armed"] is True


def test_accepted_report_baseline_is_shared_with_monitor():
    value = market()
    reference = report_reference(value, "2026-10-15", NOW)
    assert reference and reference["version"] == VERSION
    state = newer_reference({}, reference, "2026-10-15")
    assert transition(value, state, NOW + timedelta(minutes=1))[1] is None
    assert newer_reference(state, reference, "2026-10-16") == {}


def test_morning_report_market_first_without_actions_and_later_material_change():
    snap = {"run_id": "sample", "trading_day": {"date": "2026-10-15"},
            "as_of": {"captured_at": NOW.isoformat()},
            "quotes": {"captured_at": NOW.isoformat(), "rows": []},
            "holdings": {"status": "complete", "count": 0, "rows": []},
            "trade_plans": {"holding_actions_authority": {"status": "current",
                "critical_alert_gate": "disabled_missing_authoritative_inputs"},
                "holding_actions": []},
            "formal_selection": {"status": "missing"},
            "post_close_review": {"due": False}, "market_risk": market()}
    title, body = render(snap, include_selection=False)
    assert body.splitlines()[0].startswith("市场（10:15）：")
    assert "市场明显走弱" in body and "账户：已核验0/0" in body
    payload = qq_payload(snap, include_selection=False)
    assert payload["original_lines"] == len(body.splitlines())
    assert payload["parts"][0] == (title, body)
    later = dict(snap, as_of={"captured_at": "2026-10-15T11:25:20+08:00"},
                 quotes={"captured_at": "2026-10-15T11:25:20+08:00", "rows": []},
                 market_risk_reference=report_reference(market(), "2026-10-15", NOW))
    later["market_risk"] = market(NOW + timedelta(minutes=70))
    assert render(later, include_selection=False)[1] == ""
    later["market_risk"] = market(NOW + timedelta(minutes=70), (-2.7, -2.0, -2.8))
    assert "市场（11:25）" in render(later, include_selection=False)[1]
    account_only = report_reference({"version": VERSION, "rows": []},
                                    "2026-10-15", NOW,
                                    {"status": "current", "total": 2, "covered": 0,
                                     "down": 0, "risk_observations": 0})
    assert account_only and account_only["indices"] == {}
    later["market_risk_reference"] = account_only
    later["market_risk"] = {"status": "unavailable", "rows": []}
    later["holdings"] = {"status": "complete", "count": 2, "rows": [
        {"symbol": "510001", "name": "ETF"}, {"symbol": "160001", "name": "LOF"}]}
    later["quotes"]["rows"] = [
        {"symbol": symbol, "name": name, "price": 1.0, "change_pct": -3,
         "freshness": "actionable", "as_of": "2026-10-15T11:25:10+08:00"}
        for symbol, name in (("510001", "ETF"), ("160001", "LOF"))]
    assert "其中下跌2只" in render(later, include_selection=False)[1]


def test_market_category_routes_once_and_does_not_open_stock_critical_gate(tmp_path, monkeypatch):
    store = ResearchStore(str(tmp_path / "archive.db"), connect_fn=sqlite3.connect,
                          is_postgres=False, ensure_schema=True)
    archive = MessageArchiveService(store=store, key=Fernet.generate_key())
    monkeypatch.setattr(notification_router, "archive_action", lambda action, body: (
        archive.prepare(**body) if action == "prepare" else
        {"started": archive.start(body["delivery_id"])} if action == "start" else
        {"recorded": archive.finish(**body)}))
    calls = []
    monkeypatch.setitem(notification_router.CHANNELS, "qq", lambda _title, body: (
        calls.append(body) or (True, "HTTP 200")))
    event = {"policy_version": VERSION, "trade_date": "2026-10-15", "episode": 1,
             "level": 1, "indices": {"上证指数": -1.6}, "as_of": NOW.isoformat()}
    kwargs = dict(source="jobs.intraday_decision_monitor", only_channels=["qq"],
                  idempotency_key="market-risk:2026-10-15:1", market_event=event)
    first = notification_router.send("market_risk", "市场风险提示", "上证指数明显下跌", **kwargs)
    repeated = notification_router.send("market_risk", "市场风险提示", "上证指数明显下跌", **kwargs)
    assert first["qq"] == (True, "HTTP 200") and first.archive_status == "recorded"
    assert repeated["qq"] == (False, "archive_suppressed")
    assert calls == ["上证指数明显下跌"]
    blocked = notification_router.send("alert", "卖出", "示例",
        source="jobs.intraday_decision_monitor", only_channels=["qq"],
        idempotency_key="market-risk:2026-10-15:2", market_event=event)
    assert not blocked and blocked.policy_status == "silent_critical_only"


def test_market_alert_survives_missing_formal_plan_without_stock_action():
    saved = {}
    calls = []
    def load(key):
        return saved.get(key, {})
    def save(key, value):
        saved[key] = value
    def notify(title, body):
        calls.append((title, body))
        result = notification_router.DeliveryResults()
        result["qq"] = (True, "HTTP 200")
        result.archive_status = "recorded"
        return result
    def cycle(at, changes):
        names = list(INDEX_CODES)[:3]
        raw = {INDEX_CODES[name][2:]: {"price": 3500, "change_pct": pct,
                                      "quote_time": at.strftime("%Y%m%d%H%M%S")}
               for name, pct in zip(names, changes)}
        return monitor.run_cycle(
            now=at, formal_loader=lambda: {}, holdings_loader=lambda: [],
            snapshot_loader=load, snapshot_saver=save,
            market_loader=lambda _codes: raw, notify_fn=notify)
    assert cycle(NOW, (-0.1, -0.2, -0.3))["status"] == "skipped"
    result = cycle(NOW + timedelta(minutes=20), (-1.6, -2.0, -2.8))
    assert result["reason"] == "today_formal_selection_unavailable"
    assert len(calls) == 1 and "市场明显走弱" in calls[0][1]
    assert saved[monitor.SNAPSHOT_KEY]["market_risk_state"]["accepted_level"] >= 1
    cycle(NOW + timedelta(minutes=21), (-1.6, -2.0, -2.8))
    assert len(calls) == 1


def test_real_rendered_large_market_report_keeps_first_part_and_all_actions(tmp_path, monkeypatch):
    at = NOW.isoformat()
    snapshot = {
        "run_id": "large-report", "trading_day": {"date": "2026-10-15"},
        "as_of": {"captured_at": at},
        "quotes": {"captured_at": at, "rows": []},
        "holdings": {"status": "complete", "count": 0, "rows": []},
        "formal_selection": {"status": "missing"},
        "post_close_review": {"due": False},
        "market_risk": market(),
        "trade_plans": {"holding_actions_authority": {"status": "current",
            "critical_alert_gate": "disabled_missing_authoritative_inputs"},
            "holding_actions": [{"symbol": f"300{i:03d}", "name": "风险观察",
                                 "action": "sell", "reason": "止损"}
                                for i in range(80)]},
    }
    payload = qq_payload(snapshot, include_selection=False)
    assert payload["part_count"] > 1
    assert "市场明显走弱" in payload["parts"][0][1]
    joined = "\n".join(body for _, body in payload["parts"])
    assert all(f"300{i:03d}｜" in joined for i in range(80))
    assert payload["original_lines"] == len(payload["content"].splitlines())
    assert all(len(body) <= 1500 and len(body.encode("utf-8")) <= 4096
               and len(body.splitlines()) <= 100 for _, body in payload["parts"])
    slot = "2026-10-15T10:15+08:00"
    claim = NotificationClaimReq(notification_slot=slot,
        payload_hash=payload["payload_hash"], original_lines=payload["original_lines"],
        delivered_lines=payload["delivered_lines"], part_count=payload["part_count"],
        category=payload["category"], version=payload["version"])
    store = ResearchStore(str(tmp_path / "ledger.db"), connect_fn=sqlite3.connect,
                          is_postgres=False, ensure_schema=True)
    service = ScheduledNotificationService(store=store,
        clock=lambda: NOW + timedelta(seconds=10))
    assert service.claim(slot=slot, payload_hash=claim.payload_hash,
        original_lines=claim.original_lines, delivered_lines=claim.delivered_lines,
        part_count=claim.part_count, category=claim.category, version=claim.version,
        actor_id="writer")["should_send"]
    assert service.start(slot=slot, payload_hash=claim.payload_hash, actor_id="writer")["started"]
    saved = []
    from jobs import jobs_hub
    monkeypatch.setattr(jobs_hub, "save_indicator_snapshot",
                        lambda key, value: saved.append((key, value)))
    finish = NotificationFinishReq(notification_slot=slot,
        payload_hash=payload["payload_hash"], status="delivered", http_status=200,
        accepted_part_count=payload["part_count"], message_archive_status="recorded",
        market_context=snapshot["market_risk"],
        account_context={"status": "current", "total": 0, "covered": 0,
                         "down": 0, "risk_observations": 0})
    result = service.finish(slot=slot, payload_hash=finish.payload_hash,
        status=finish.status, http_status=finish.http_status,
        accepted_part_count=finish.accepted_part_count,
        message_archive_status=finish.message_archive_status,
        market_context=finish.market_context, account_context=finish.account_context,
        actor_id="writer")
    assert result["recorded"] and result["market_reference_recorded"]
    assert saved[0][0] == "_scheduled_market_risk_reference"
    assert saved[0][1]["indices"]["上证指数"] == -1.6
    assert service.claim(slot=slot, payload_hash=claim.payload_hash,
        original_lines=claim.original_lines, delivered_lines=claim.delivered_lines,
        part_count=claim.part_count, category=claim.category, version=claim.version,
        actor_id="writer")["prior_sent"]
