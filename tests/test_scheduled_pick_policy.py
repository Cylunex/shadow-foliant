"""Rendered payload, protected ledger and source identity stay in agreement."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import sqlite3
from zoneinfo import ZoneInfo

from application.scheduled_notification import ScheduledNotificationService
from application.scheduled_pick_policy import decide_selection, selection_result
from data.research_store import ResearchStore
from scripts import foliant_scheduled_snapshot as cli
from scripts.scheduled_qq_format import render_parts


DAY = "2026-10-14"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def sample(day: str = DAY, time: str = "10:15") -> dict:
    at = f"{day}T{time}:20+08:00"
    formal = [{"symbol": f"00000{i}", "name": f"正式{i}"} for i in range(1, 6)]
    independent = [{"symbol": f"60000{i}", "name": f"独立{i}"} for i in range(1, 6)]
    return {
        "run_id": "report-1", "trading_day": {"date": day},
        "as_of": {"captured_at": at},
        "formal_selection": {"status": "complete", "selection_date": day,
                             "run_id": "formal-1", "market_as_of": day,
                             "formal_top5": formal},
        "independent_selection": {"status": "complete",
                                  "selection_session_date": day,
                                  "input_snapshot_id": "independent-1",
                                  "top5": independent},
        "external_independent_research": {"status": "missing"},
        "quotes": {"captured_at": at, "rows": [
            {"symbol": row["symbol"], "name": row["name"], "price": 10 + i,
             "freshness": "actionable", "as_of": at}
            for i, row in enumerate(formal + independent)]},
        "trade_plans": {"holding_actions_authority": {"status": "current"},
                        "holding_actions": []},
        "post_close_review": {"due": False},
    }


def ledger(tmp_path):
    now = [datetime.fromisoformat(DAY + "T10:15:30+08:00")]
    store = ResearchStore(str(tmp_path / "research.db"), connect_fn=sqlite3.connect,
                          is_postgres=False, ensure_schema=True)
    return ScheduledNotificationService(store=store, clock=lambda: now[0]), now


def choose(service, snapshot, time):
    slot = f"{snapshot['trading_day']['date']}T{time}+08:00"
    state = service.selection_state(slot=slot, actor_id="writer")
    current = selection_result(snapshot)
    include = decide_selection(slot, current, state)
    payload = cli.qq_payload(snapshot, include_selection=include,
                             selection_updated=include and time != "10:15")
    return slot, current, include, payload


def claim_and_finish(service, slot, current, include, payload, status="delivered",
                     accepted=None, http=200, archive="recorded"):
    data = service.claim(slot=slot, payload_hash=payload["payload_hash"],
                         original_lines=payload["original_lines"],
                         delivered_lines=payload["delivered_lines"],
                         part_count=payload["part_count"], category="report",
                         version=payload["version"], actor_id="writer",
                         pick_fingerprint=current.get("fingerprint"),
                         pick_origin_fingerprint=current.get("origin_fingerprint"),
                         pick_selection_run_id=current.get("selection_run_id"),
                         pick_research_id=current.get("research_id"),
                         pick_sources=current.get("sources") or "",
                         pick_included=include)
    assert data["should_send"]
    assert service.start(slot=slot, payload_hash=payload["payload_hash"],
                         actor_id="writer")["started"]
    accepted = payload["part_count"] if accepted is None else accepted
    assert service.finish(slot=slot, payload_hash=payload["payload_hash"],
                          actor_id="writer", status=status, http_status=http,
                          accepted_part_count=accepted,
                          message_archive_status=archive)["recorded"]


def test_four_slots_change_only_silent_and_next_day(tmp_path):
    service, now = ledger(tmp_path)
    morning = sample()
    slot, picks, include, payload = choose(service, morning, "10:15")
    assert include and "选股（" in payload["content"]
    assert payload["part_count"] == 1
    claim_and_finish(service, slot, picks, include, payload)
    # Reconstruct the service as a rotated worker would; the accepted result
    # comes from the database, never from process memory.
    service = ScheduledNotificationService(store=service.store, clock=lambda: now[0])
    state = service.selection_state(slot=DAY + "T11:25+08:00", actor_id="writer")
    assert state["accepted_fingerprint"] == picks["fingerprint"]
    assert state["accepted_slot"] == slot
    assert state["accepted_selection_run_id"] == "formal-1"
    assert state["accepted_sources"] == "正式+独立量化"

    midday = deepcopy(morning)
    midday["as_of"]["captured_at"] = DAY + "T11:25:20+08:00"
    midday["quotes"]["captured_at"] = DAY + "T11:25:20+08:00"
    midday["quotes"]["rows"][0]["price"] = 90
    midday["formal_selection"]["run_id"] = "formal-2"
    midday["formal_selection"]["formal_top5"][0]["score"] = 999
    now[0] = datetime.fromisoformat(DAY + "T11:25:30+08:00")
    slot, picks2, include, payload = choose(service, midday, "11:25")
    assert picks2["fingerprint"] == picks["fingerprint"]
    assert not include and payload == {"silent": True}
    assert service.record_silent(slot=slot, actor_id="writer")["recorded"]
    assert next(row for row in service.audit(actor_id="writer")
                if row["notification_slot"] == slot)["delivery_status"] == "silent"

    afternoon = deepcopy(midday)
    afternoon["formal_selection"]["formal_top5"][0] = {
        "symbol": "000099", "name": "新增"}
    afternoon["trade_plans"]["holding_actions"] = [
        {"symbol": "000001", "name": "风险", "action": "sell",
         "reason": "止损", "decision_source": "hard_risk"}]
    now[0] = datetime.fromisoformat(DAY + "T14:35:30+08:00")
    slot, changed, include, payload = choose(service, afternoon, "14:35")
    assert include and "选股更新：" in payload["content"]
    assert payload["content"].index("持仓操作") < payload["content"].index("选股（")
    claim_and_finish(service, slot, changed, include, payload)
    assert service.record_silent(slot=slot, actor_id="writer")["prior_sent"]

    evening = deepcopy(afternoon)
    evening["post_close_review"] = {"due": True}
    evening["holdings_review"] = {"status": "complete", "count": 2,
                                  "reviewed_count": 2, "rows": []}
    evening["next_session_plan"] = {"status": "degraded", "count": 3,
                                    "ready_count": 2}
    now[0] = datetime.fromisoformat(DAY + "T20:45:30+08:00")
    slot, _, include, payload = choose(service, evening, "20:45")
    assert not include and "选股（" not in payload["content"]
    assert "盘后复盘：持仓复核2/2；次日计划2/3可用" in payload["content"]

    next_day = sample("2026-10-15")
    now[0] = datetime.fromisoformat("2026-10-15T10:15:30+08:00")
    slot, _, include, payload = choose(service, next_day, "10:15")
    assert include and "选股（" in payload["content"]


def test_failed_partial_unknown_and_source_gaps_do_not_invent_baseline(tmp_path):
    service, now = ledger(tmp_path)
    morning = sample()
    slot, picks, include, payload = choose(service, morning, "10:15")
    claim_and_finish(service, slot, picks, include, payload, status="failed",
                     accepted=0, http=500, archive="unrecorded")
    now[0] = datetime.fromisoformat(DAY + "T11:25:30+08:00")
    assert not choose(service, sample(time="11:25"), "11:25")[2]
    changed = sample(time="11:25")
    changed["formal_selection"]["run_id"] = "formal-2"
    changed["formal_selection"]["formal_top5"].reverse()
    slot, new, include, payload = choose(service, changed, "11:25")
    assert include
    claim_and_finish(service, slot, new, include, payload, status="unknown",
                     accepted=payload["part_count"], http=200, archive="unknown")
    state = service.selection_state(slot=DAY + "T14:35+08:00", actor_id="writer")
    assert state["accepted_fingerprint"] == new["fingerprint"]
    assert not state["uncertain_pick_attempt"]

    missing = deepcopy(changed)
    missing["independent_selection"]["status"] = "missing"
    missing["formal_selection"]["run_id"] = "formal-3"
    missing["formal_selection"]["formal_top5"].reverse()
    assert not choose(service, missing, "14:35")[2]

    another = deepcopy(changed)
    another["formal_selection"]["run_id"] = "formal-3"
    another["formal_selection"]["formal_top5"].reverse()
    now[0] = datetime.fromisoformat(DAY + "T14:35:30+08:00")
    slot, current, include, payload = choose(service, another, "14:35")
    assert include
    # A started attempt with no confirmed receipt is ambiguous after restart.
    service.claim(slot=slot, payload_hash=payload["payload_hash"],
                  original_lines=payload["original_lines"],
                  delivered_lines=payload["delivered_lines"],
                  part_count=payload["part_count"], category="report",
                  version=payload["version"], actor_id="writer",
                  pick_fingerprint=current["fingerprint"],
                  pick_origin_fingerprint=current["origin_fingerprint"],
                  pick_selection_run_id=current["selection_run_id"],
                  pick_research_id=current["research_id"],
                  pick_sources=current["sources"], pick_included=True)
    service.start(slot=slot, payload_hash=payload["payload_hash"], actor_id="writer")
    assert not service.claim(slot=slot, payload_hash=payload["payload_hash"],
                             original_lines=payload["original_lines"],
                             delivered_lines=payload["delivered_lines"],
                             part_count=payload["part_count"], category="report",
                             version=payload["version"], actor_id="writer",
                             pick_fingerprint=current["fingerprint"],
                             pick_origin_fingerprint=current["origin_fingerprint"],
                             pick_selection_run_id=current["selection_run_id"],
                             pick_research_id=current["research_id"],
                             pick_sources=current["sources"], pick_included=True)["should_send"]
    assert service.selection_state(slot=DAY + "T20:45+08:00",
                                   actor_id="writer")["uncertain_pick_attempt"]


def test_partial_acceptance_never_advances_daily_pick_baseline(tmp_path):
    service, now = ledger(tmp_path)
    morning = sample()
    morning["trade_plans"]["holding_actions"] = [
        {"symbol": f"300{i:03d}", "name": "风险", "action": "reduce",
         "reason": "组合风险"} for i in range(65)]
    slot, picks, include, payload = choose(service, morning, "10:15")
    assert payload["part_count"] > 1
    claim_and_finish(service, slot, picks, include, payload, status="partial",
                     accepted=1, http=500, archive="partial")
    state = service.selection_state(slot=DAY + "T11:25+08:00", actor_id="writer")
    assert state["accepted_fingerprint"] is None
    assert state["uncertain_pick_attempt"]
    later = sample(time="11:25")
    later["formal_selection"]["run_id"] = "formal-2"
    later["formal_selection"]["formal_top5"].reverse()
    now[0] = datetime.fromisoformat(DAY + "T11:25:30+08:00")
    assert not choose(service, later, "11:25")[2]


def test_valid_external_source_change_and_rendered_split(tmp_path):
    service, now = ledger(tmp_path)
    morning = sample()
    morning["external_independent_research"] = {
        "status": "complete", "overlay_id": "overlay-1",
        "selection_run_id": "formal-1",
        "top5": [{"symbol": "600001", "name": "独立1"}]}
    slot, picks, include, payload = choose(service, morning, "10:15")
    claim_and_finish(service, slot, picks, include, payload)
    later = deepcopy(morning)
    later["external_independent_research"]["status"] = "missing"
    assert not choose(service, later, "11:25")[2]
    later["external_independent_research"] = {
        "status": "complete", "overlay_id": "overlay-2",
        "selection_run_id": "formal-1",
        "top5": [{"symbol": "600002", "name": "独立2"}]}
    now[0] = datetime.fromisoformat(DAY + "T11:25:30+08:00")
    slot, new, include, payload = choose(service, later, "11:25")
    assert include and "外部研究" in payload["content"]
    claim_and_finish(service, slot, new, include, payload)

    many = deepcopy(later)
    many["trade_plans"]["holding_actions"] = [
        {"symbol": f"300{i:03d}", "name": "风险观察", "action": "reduce",
         "reason": "组合风险"} for i in range(65)]
    title, body = cli.render_qq_summary(many, include_selection=False)
    parts = render_parts(title, body)
    assert len(parts) > 1
    joined = "\n".join(part for _, part in parts)
    assert all(f"300{i:03d}｜" in joined for i in range(65))
    assert all(len(part.encode("utf-8")) <= 4096 for _, part in parts)
    assert all("编号｜名称｜现价｜操作｜原因" in part for _, part in parts)


def test_rendered_parts_use_one_slot_and_each_archive_is_checked(monkeypatch):
    from notify import notification_router

    snapshot = sample()
    snapshot.update({"schema_version": "scheduled-agent-snapshot-v1",
                     "status": "complete",
                     "holdings": {"status": "complete"},
                     "holdings_review": {"status": "pending"},
                     "next_session_plan": {"status": "pending"},
                     "quality": {"status": "complete"},
                     "trading_day": {"date": DAY, "confirmed": True,
                                     "is_trading_day": True},
                     "notification_policy": {
                         "version": "critical-only-action-required-v1",
                         "report_delivery": "scheduled_slot_report",
                         "critical_event_delivery": "intraday_monitor_only",
                         "scheduled_delivery": "silent_monitor_owns_critical_events"}})
    snapshot["trade_plans"]["holding_actions"] = [
        {"symbol": f"300{i:03d}", "name": "风险观察", "action": "reduce",
         "reason": "组合风险"} for i in range(65)]
    payload = cli.qq_payload(snapshot, include_selection=False)
    assert payload["part_count"] > 1
    monkeypatch.setenv("QQ_WEBHOOK_URL", "https://example.invalid/qq")
    calls = []

    def accepted(*args, **kwargs):
        calls.append((args, kwargs))
        result = notification_router.DeliveryResults()
        result["qq"] = (True, "HTTP 200")
        result.archive_status = "recorded"
        result.message_id = f"{len(calls):032x}"
        return result

    monkeypatch.setattr(notification_router, "send", accepted)
    slot = DAY + "T14:35+08:00"
    delivered = cli.send_qq(snapshot, payload=payload, notification_slot=slot)
    assert delivered["delivery_status"] == "delivered"
    assert delivered["accepted_part_count"] == payload["part_count"]
    assert delivered["message_archive_status"] == "recorded"
    assert [kwargs["idempotency_key"] for _, kwargs in calls] == [
        f"scheduled-qq:{slot}:{payload['payload_hash'][:16]}:{i}"
        for i in range(1, payload["part_count"] + 1)]
    assert all(kwargs["original_body"] for _, kwargs in calls)
    assert all(args[0] == "report" for args, _ in calls)

    calls.clear()

    def second_fails(*args, **kwargs):
        calls.append((args, kwargs))
        result = notification_router.DeliveryResults()
        result["qq"] = (True, "HTTP 200") if len(calls) == 1 else (False, "HTTP 500")
        result.archive_status = "recorded" if len(calls) == 1 else "unrecorded"
        result.message_id = "a" * 32 if len(calls) == 1 else None
        return result

    monkeypatch.setattr(notification_router, "send", second_fails)
    partial = cli.send_qq(snapshot, payload=payload, notification_slot=slot)
    assert partial["delivery_status"] == "partial"
    assert partial["accepted_part_count"] == 1
    assert partial["message_archive_status"] == "partial"
    assert len(calls) == 2


def test_cli_empty_midday_uses_silent_record_without_claim(monkeypatch, capsys):
    snapshot = sample(time="11:25")
    snapshot["schema_version"] = "scheduled-agent-snapshot-v1"
    snapshot["status"] = "complete"
    picks = selection_result(snapshot)
    monkeypatch.setattr(cli, "fetch_snapshot", lambda: deepcopy(snapshot))
    monkeypatch.setattr(cli, "_degrade_external_submission",
                        lambda value, *_args, **_kwargs: value)
    monkeypatch.setattr(cli, "qq_preflight", lambda *_args: None)
    monkeypatch.setattr(cli, "scheduled_notification_slot", lambda *_args, **_kwargs:
                        DAY + "T11:25+08:00")
    monkeypatch.setattr(cli, "fetch_selection_state", lambda _slot: ({
        "policy_version": "morning-picks-change-only-v1",
        "accepted_fingerprint": picks["fingerprint"],
        "accepted_origin_fingerprint": picks["origin_fingerprint"],
        "accepted_sources": picks["sources"],
    }, None))
    calls = []

    def ledger(action, _body):
        calls.append(action)
        assert action == "silent"
        return {"data": {"recorded": True}}, None

    monkeypatch.setattr(cli, "notification_ledger", ledger)
    monkeypatch.setattr(cli, "send_qq", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("unexpected QQ send")))
    assert cli.main(["--send-qq", "--notification-slot", "11:25",
                     "--delivery-receipt"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert calls == ["silent"]
    assert receipt["notification"]["delivery_status"] == "silent"
    assert receipt["notification"]["delivery_recorded"] is True
