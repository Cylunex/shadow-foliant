import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from data.research_store import ResearchStore
from application.portfolio_daily import build_daily, DailyExitService
from application.losna_research import LosnaResearch
from notify.plain_language import compact_notification

NOW = datetime(2026, 11, 8, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def store(tmp_path):
    return ResearchStore(str(tmp_path / "test.db"), connect_fn=sqlite3.connect)


def test_equity_limit_etf_weekend_and_missing_prices(store):
    holdings = [{"code": f"600{i:03}", "quantity": 100} for i in range(16)]
    holdings += [{"code": "510001", "quantity": 200}, {"code": "600000", "quantity": 50},
                 {"code": "600999", "quantity": 0}]
    metadata = {"stock_symbols": {h["code"] for h in holdings[:16]},
                "etf_symbols": {"510001"}, "fund_symbols": {"510001"}}
    report = build_daily(holdings, metadata, [], now=NOW)
    assert report["counts"]["equity_count"] == 16
    assert report["counts"]["etf_count"] == 1
    assert report["required"]
    assert compact_notification("report", report["text"]) == report["text"]
    calls = []
    def sender(*args, **kw):
        calls.append(kw)
        return {"qq": (True, "HTTP 200")}
    service = DailyExitService(store)
    assert service.deliver(report, sender)["status"] == "accepted"
    assert service.deliver(report, sender)["status"] == "accepted"
    assert len(calls) == 1
    report["created_at"] = (NOW + timedelta(days=1)).isoformat()
    service.deliver(report, sender)
    assert len(calls) == 2
    holdings.pop(0)
    report = build_daily(holdings, metadata, [], now=NOW)
    # Duplicate batch of the same symbol remains an active position.
    assert report["counts"]["equity_count"] == 16
    report = build_daily(holdings[1:], metadata, [], now=NOW)
    assert not report["required"]


def test_unknown_delivery_does_not_repeat(store):
    r = build_daily([{"code": "600001", "quantity": None}], {}, [], now=NOW)
    service = DailyExitService(store)
    def fail(*a, **k):
        raise TimeoutError()
    assert service.deliver(r, fail)["status"] == "unknown"
    assert service.deliver(r, lambda *a, **k: pytest.fail("duplicate"))["status"] == "unknown"


def test_losna_scope_immutable_receipt_and_deadline(store):
    service = LosnaResearch(store, lambda: NOW)
    job = service.create(purpose="directed_review", input_version="v1",
                         candidates=[{"symbol": "600001"}],
                         evidence=[{"evidence_id": "e1", "fact": "原始事实"}], owner="research")
    claimed = service.claim(owner="research", worker="losna", purposes=["directed_review"])
    assert service.claim(owner="research", worker="other", purposes=["directed_review"])["status"] == "empty"
    with pytest.raises(ValueError):
        service.get(job["object_id"], "private")
    result = {"reviews": [{"symbol": "600001", "reason": "证据支持观察",
                          "counterevidence": "需要继续核对", "evidence_refs": ["e1"]}], "next_check": "午间"}
    bad = {**result, "reviews": [{**result["reviews"][0], "evidence_refs": ["private-secret"]}]}
    args = dict(identity=job["object_id"], owner="research", worker="losna", generation=claimed["revision"])
    with pytest.raises(ValueError, match="evidence"):
        service.submit(**args, result=bad)
    receipt = service.submit(**args, result=result)
    assert receipt["status"] == "received" and not receipt["auto_apply"]
    assert service.submit(**args, result=result) == receipt
    with pytest.raises(ValueError, match="conflict"):
        service.submit(**args, result={**result, "next_check": "明天"})
    another = service.create(purpose="directed_review", input_version="v2",
                             candidates=[], evidence=[], owner="research")
    late = service.claim(owner="research", worker="losna", purposes=["directed_review"])
    service.clock = lambda: NOW + timedelta(hours=1)
    with pytest.raises(ValueError, match="expired"):
        service.submit(identity=another["object_id"], owner="research", worker="losna",
                       generation=late["revision"], result={"reviews": [], "next_check": "明天"})


def test_real_daily_payload_archives_and_retries_only_explicit_rejection(store, monkeypatch):
    from cryptography.fernet import Fernet
    from application.message_archive import MessageArchiveService
    from notify import notification_router as router
    archive = MessageArchiveService(store=store, key=Fernet.generate_key())
    def action(name, body):
        if name == "prepare":
            return archive.prepare(**body)
        if name == "start":
            return {"started": archive.start(body["delivery_id"])}
        return {"recorded": archive.finish(**body)}
    monkeypatch.setattr(router, "archive_action", action)
    calls = []
    def channel(title, body):
        calls.append(body)
        return (False, "HTTP 429") if len(calls) == 1 else (True, "HTTP 200")
    monkeypatch.setitem(router.CHANNELS, "qq", channel)
    def sender(title, body, **kw):
        kw.setdefault("only_channels", ["qq"])
        return router.send("report", title, body, **kw)
    holdings = [{"code": f"600{i:03}", "quantity": 100} for i in range(150)]
    metadata = {"stock_symbols": {r["code"] for r in holdings}}
    decisions = [{"symbol": h["code"], "name": "很长的示例名称" * 4,
                  "action": "sell", "reason": "原计划失效需要复核" * 10} for h in holdings]
    report = build_daily(holdings, metadata, decisions, now=NOW)
    service = DailyExitService(store)
    assert service.deliver(report, sender)["status"] == "retryable"
    result = service.deliver(report, sender)
    assert result["status"] == "accepted"
    assert service.deliver(report, sender)["status"] == "accepted"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert all("150/15" in body and "ETF 0" in body for body in calls)


def test_fixed_summary_preserves_five_levels_and_equity_counts():
    from jobs.intraday_decision_monitor import format_fixed_summary
    from application.market_risk_notice import current_indices, INDEX_CODES
    market = current_indices(NOW, lambda codes: {
        code: {"price": 3600 + i, "change_pct": i - 2,
               "quote_time": NOW.strftime("%Y%m%d%H%M%S")}
        for i, code in enumerate(INDEX_CODES.values())})
    snapshot = {"market_risk": market, "position_counts": {
        "equity_count": 16, "excess": 1, "etf_count": 3,
        "other_fund_count": 1, "unknown_count": 0}}
    body = compact_notification("report", format_fixed_summary(snapshot, "10:05"))
    for i, name in enumerate(INDEX_CODES):
        assert name in body and f"{3600+i:,.2f}点" in body
    assert "16/15" in body and "ETF 3" in body


def test_blind_packet_uses_manifest_features_not_published_membership(store, monkeypatch):
    import pandas as pd
    from types import SimpleNamespace
    from analysis import independent_selector
    from application.losna_research import prepare_discovery, KIND
    monkeypatch.setattr(independent_selector, "_prepare_frame",
                        lambda *args: (pd.DataFrame({"symbol": ["600001", "600002"],
                            "pe_ttm": [8, 9], "cash_quality": [1, 1], "ret_60": [-.1, .2]}), {}))
    monkeypatch.setattr(independent_selector, "_score", lambda frame, policy: (frame, {}))
    monkeypatch.setattr(store, "latest_formal_selection",
                        lambda: pytest.fail("blind discovery read formal selection"))
    job = prepare_discovery(SimpleNamespace(store=store), {"manifest_id": "m1", "run_id": "r1"})
    assert [r["symbol"] for r in job["packet"]["candidates"]] == ["600001"]
    assert job["packet"]["purpose"] == "independent_discovery"
    assert not any("formal_rank" in row for row in job["packet"]["candidates"])


def test_blind_nominations_use_existing_pipeline_without_activating_policy(store):
    from application.losna_research import register_forward_nominations
    store.save_selection({"run_id": "fixture-run", "status": "failed", "selection_date": "2026-11-08"}, [], [])
    service = LosnaResearch(store, lambda: NOW)
    job = service.create(purpose="independent_discovery", input_version="fixture-v1",
                         candidates=[{"symbol": "600001"}], evidence=[{"evidence_id": "e1"}],
                         owner="research", run_id="fixture-run")
    claim = service.claim(owner="research", worker="losna", purposes=["independent_discovery"])
    service.submit(identity=job["object_id"], owner="research", worker="losna", generation=claim["revision"],
                   result={"reviews": [{"symbol": "600001", "reason": "观察", "counterevidence": "待核",
                                        "evidence_refs": ["e1"]}], "next_check": "午间"})
    assert register_forward_nominations(store) == 1
    assert register_forward_nominations(store) == 0
    conn = store.connect()
    try:
        assert conn.execute("SELECT strategy_id FROM selection_candidate_nominations").fetchone()[0] == "losna_blind"
        assert conn.execute("SELECT COUNT(*) FROM strategy_policy_versions").fetchone()[0] == 0
    finally:
        conn.close()


def test_rejected_daily_refreshes_holdings_and_closes_obligation(store):
    holdings = [{"code": f"600{i:03}", "quantity": 100} for i in range(17)]
    metadata = {"stock_symbols": {r["code"] for r in holdings}}
    service = DailyExitService(store)
    reports = []
    def reject(title, text, **kw):
        reports.append(text)
        return {"qq": (False, "HTTP 429")}
    first = build_daily(holdings, metadata, [], now=NOW)
    service.deliver(first, reject)
    service.deliver(build_daily(holdings[1:], metadata, [], now=NOW), reject)
    assert "17/15" in reports[0] and "16/15" in reports[1]
    assert service.deliver(build_daily(holdings[2:], metadata, [], now=NOW), reject)["status"] == "not_required"
    assert service.repo.list("daily_equity_exit", owner="primary")[0]["status"] == "closed"


def test_research_claim_lease_fences_old_worker_and_expires(store):
    clock = [NOW]
    service = LosnaResearch(store, lambda: clock[0])
    job = service.create(purpose="directed_review", input_version="lease", candidates=[], evidence=[], owner="research")
    old = service.claim(owner="research", worker="losna", purposes=["directed_review"], claim_id="run1")
    assert service.claim(owner="research", worker="losna", purposes=["directed_review"], claim_id="run2")["status"] == "empty"
    clock[0] += timedelta(minutes=4)
    new = service.claim(owner="research", worker="losna", purposes=["directed_review"], claim_id="run2")
    assert new["revision"] > old["revision"]
    with pytest.raises(ValueError, match="generation"):
        service.submit(identity=job["object_id"], owner="research", worker="losna", generation=old["revision"], result={"reviews": [], "next_check": "核对"})
    clock[0] += timedelta(hours=1)
    assert service.claim(owner="research", worker="losna", purposes=["directed_review"])["status"] == "empty"
    assert service.get(job["object_id"], "research")["status"] == "expired"


def test_preopen_indices_retain_confirmed_close_without_current_risk():
    from application.market_risk_notice import current_indices, index_lines, INDEX_CODES
    before = NOW.replace(hour=9, minute=0)
    close = before - timedelta(days=2)
    close = close.replace(hour=15, minute=0)
    market = current_indices(before, lambda codes: {code: {"price": 3600, "change_pct": -2,
        "quote_time": close.strftime("%Y%m%d%H%M%S")} for code in codes}, prior_close=True)
    assert market["status"] == "prior_close" and len(market["rows"]) == 5
    assert "已核收盘" in index_lines(market)[0]
    assert close.strftime("%Y-%m-%d") in index_lines(market)[0]
    assert current_indices(before, lambda codes: {code: {"price": 3600, "change_pct": -2,
        "quote_time": close.strftime("%Y%m%d%H%M%S")} for code in codes})["status"] == "unavailable"


def test_unknown_pnl_does_not_create_flat_position_or_health_fact():
    from portfolio.exit_advisor import _exit_score
    _, category, action, reasons = _exit_score({"pnl": None, "change": None}, 120)
    assert category == "证据待核" and action == "hold"
    assert not any("横盘" in text and "判定" not in text for text in reasons)


def test_native_daily_job_adapts_report_router_signature(store, monkeypatch):
    from application import portfolio_daily
    from notify import notification_router
    from jobs import jobs_hub
    import portfolio_db
    from application import stock_budget
    import data.research_store
    holdings = [{"code": f"600{i:03}", "quantity": 100} for i in range(16)]
    monkeypatch.setattr(data.research_store, "ResearchStore", lambda **kwargs: store)
    monkeypatch.setattr(portfolio_db.portfolio_db, "action_preview_context", lambda **kwargs: {"holdings": holdings, "watermark": "v1"})
    monkeypatch.setattr(stock_budget, "load_security_metadata", lambda *a: {"stock_symbols": {r["code"] for r in holdings}})
    monkeypatch.setattr(jobs_hub, "get_indicator_snapshot", lambda key: {})
    calls = []
    def router(message_type, title, body, **kwargs):
        calls.append((message_type, title, body))
        return {"qq": (True, "HTTP 200")}
    monkeypatch.setattr(notification_router, "send", router)
    assert portfolio_daily.run_daily_exit()["status"] == "accepted"
    assert calls[0][0] == "report" and "16/15" in calls[0][2]


def test_lunch_report_keeps_verified_midday_close_levels():
    from application.market_risk_notice import current_indices, index_lines, INDEX_CODES
    now = NOW.replace(hour=12, minute=0)
    quote_time = now.replace(hour=11, minute=30).strftime("%Y%m%d%H%M%S")
    market = current_indices(now, lambda codes: {c: {"price": 3600, "change_pct": -1.2,
        "quote_time": quote_time} for c in codes})
    assert len(market["rows"]) == 5 and market["status"] == "session_close"
    assert "午间收盘 11:30" in index_lines(market)[0]
    stale = current_indices(now, lambda codes: {c: {"price": 3600, "change_pct": -1.2,
        "quote_time": now.replace(hour=11, minute=0).strftime("%Y%m%d%H%M%S")} for c in codes})
    assert stale["status"] == "unavailable"


def test_legacy_exit_and_eod_use_equities_only_and_limit_15(monkeypatch):
    from application import portfolio_daily
    from portfolio import exit_advisor, eod_review
    import portfolio_db
    import jobs_hub
    holdings = [{"code": f"600{i:03}", "quantity": 100} for i in range(16)]
    holdings += [{"code": f"510{i:03}", "quantity": 100} for i in range(3)]
    meta = {"stock_symbols": {h["code"] for h in holdings[:16]}, "etf_symbols": {h["code"] for h in holdings[16:]}}
    monkeypatch.setattr(portfolio_db.portfolio_db, "get_all_stocks", lambda: holdings)
    monkeypatch.setattr(jobs_hub, "_scan_holdings_with_snapshot", lambda: [])
    monkeypatch.setattr(portfolio_daily, "load_account_counts", lambda rows: portfolio_daily.count_positions(rows, meta))
    monkeypatch.setattr(exit_advisor, "_ai_narrative", lambda *args: "")
    for run in [exit_advisor.run_exit_advice, eod_review.run_eod_review]:
        result = run(target_positions=20, record_signals=False)
        assert result["ok"] and result["position_counts"]["equity_count"] == 16
        assert "个股 16/15" in result["text"] and "ETF 3" in result["text"]
    del holdings[:2]
    result = exit_advisor.run_exit_advice(record_signals=False)
    assert len(holdings) == 17 and not result["over_diversified"]
    assert "个股 14/15" in result["text"]
