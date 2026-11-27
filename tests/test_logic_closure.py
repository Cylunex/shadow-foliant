"""Three-pass audit regressions: happy path, recovery, and boundary semantics."""
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
import json
import sqlite3
import pytest

from data.research_store import ResearchStore
from data.reliability_store import ReliabilityStore
from application.research_cases import ResearchCases, compile_claim, validity
from application.account_reconciliation import AccountReconciliation
from application.decision_loop import DecisionLoopService
from application.settlement_evidence import SettlementEvidence
from analysis.validation_protocol import DEFAULT_PROTOCOL, evaluate_rows
from analysis.model_ledger import empty_ledger, mark_ledger


@pytest.fixture
def store(tmp_path):
    return ResearchStore(str(tmp_path / "fixture.db"), connect_fn=sqlite3.connect)


def claim():
    return {"text": "核对数字", "expires_at": "2026-09-05T08:00:00+08:00",
        "passages": [{"source_id": "filing", "quote": "金额 100", "locator": "p1",
                      "published_at": "2026-09-01T00:00:00+08:00", "first_seen_at": "2026-09-01T01:00:00+08:00",
                      "unit": "CNY", "currency": "CNY", "basis": "consolidated", "period_kind": "annual"}],
        "calculation": {"operation": "sum", "operands": [{"passage_index": 0, "value": "100"}]}}


def test_claim_edit_preserves_and_recomputes_evidence(store):
    cases = ResearchCases(store)
    first = cases.draft("600001", owner="a", text="old", claims=[claim()])
    edited = cases.draft("600001", owner="a", text="new", claims=first["claims"], expected_revision=1)
    assert edited["claims"][0]["computed_value"] == "100"
    locked = cases.lock("600001", owner="a", draft_revision=2, human_confirmed=True)
    assert cases.lock("600001", owner="a", draft_revision=2, human_confirmed=True) == locked
    assert locked["next_check"] == "2026-09-05T00:00:00+00:00"


def test_claim_time_and_sign_are_not_string_substrings():
    compiled = compile_claim(**claim())
    assert validity([compiled], now="2026-09-04T23:59:00+00:00")["freshness"] == "current"
    assert validity([compiled], now="2026-09-05T00:01:00+00:00")["freshness"] == "stale"
    bad = claim()
    bad["passages"][0]["quote"] = "金额 -100"
    with pytest.raises(ValueError, match="operand_not_in_passage"):
        compile_claim(**bad)
    bad["expires_at"] = "2026-09-05"
    with pytest.raises(ValueError, match="timezone_required"):
        compile_claim(**bad)


def test_case_review_crash_is_visible_and_human_ack_is_private(store):
    cases = ResearchCases(store)
    event = cases.event("600001", {"event_id": "event-1", "source_id": "fixture", "published_at": "2026-09-01"})
    work = cases.repo.claim("case_review", now="2030-01-01T00:00:00+00:00")[0]
    cases.repo.put("case_investigation", work["work_id"], {"status": "reserved", "symbol": "600001"})
    value = cases.investigate(work, now="2030-01-01T00:16:00+00:00", call=lambda *a, **k: pytest.fail("must not call model twice"))
    assert value["status"] == "interrupted"
    assert cases.view()["investigations"][0]["status"] == "interrupted"
    with pytest.raises(PermissionError):
        cases.acknowledge(event["object_id"], owner="a", note="checked")
    cases.acknowledge(event["object_id"], owner="a", note="checked", human_confirmed=True)
    assert not cases.view(owner="a")["attention_top5"]
    assert cases.view()["attention_top5"] and cases.view(owner="b")["attention_top5"]


def test_seed_heals_event_after_partial_write(store):
    cases = ResearchCases(store)
    capsule = {"run_id": "r", "capsule_id": "c", "published_at": "2026-09-01T10:00:00+08:00",
               "opportunity_set": {"top15": [{"symbol": "600001"}], "top5": [{"symbol": "600001"}]}}
    cases.repo.put("case", "600001", {"symbol": "600001", "run_ids": ["r"]})
    cases.seed(capsule)
    assert cases.repo.work_status("case_review") == {"pending": 1}
    cases.seed(capsule)
    assert cases.repo.work_status("case_review") == {"pending": 1}


def test_agent_account_preview_can_be_read_confirmed_and_retried(store):
    service = AccountReconciliation(store)
    preview = service.preview([dict(external_id="d", kind="deposit", date="2026-09-01", amount="100")], owner="a", watermark="v1")
    assert service.view(owner="a")["pending_previews"][0]["object_id"] == preview["object_id"]
    assert not service.view(owner="b")["pending_previews"]
    done = service.confirm(preview["object_id"], owner="a", watermark="v1")
    assert service.confirm(preview["object_id"], owner="a", watermark="v2") == done
    assert not service.view(owner="a")["pending_previews"]


def test_account_summary_matches_requested_window(monkeypatch):
    from application.account_preview import account_books
    import portfolio.daily_pnl as daily
    rows = [dict(snap_date=f"2026-09-{i:02d}", total_daily_pnl=i, total_daily_pct=1, total_mv=100) for i in range(1, 10)]
    monkeypatch.setattr(daily, "get_recent", lambda days: rows)
    book = account_books(2)
    assert book["summary"]["period_days"] == len(book["series"]) == 2
    assert book["summary"]["period_pnl"] == 17
    assert book["summary"]["mtd_pnl"] == 45


def test_queue_budget_and_identity_are_authoritative(store):
    repo = ReliabilityStore(store)
    with pytest.raises(ValueError, match="invalid_work_budget"):
        repo.claim("x", limit=-1)
    identity = repo.enqueue("x", "one", {"work_id": "forged", "attempt": 999})
    row = repo.claim("x", now="2030-01-01T00:00:00+00:00")[0]
    assert row["work_id"] == identity and row["attempt"] == 1


def test_unknown_action_history_does_not_become_verified_when_flat():
    ledger = mark_ledger(empty_ledger(), trade_date="2026-09-01", prices={}, corporate_actions_complete=False)
    ledger.pop("unverified_action_dates")  # Upgrade from an older persisted ledger.
    ledger = mark_ledger(ledger, trade_date="2026-09-02", prices={}, corporate_actions_complete=True)
    assert ledger["marks"][-1]["status"] == "indicative"


def test_etf_quote_precision_is_not_rounded_before_valuation():
    from analysis.portfolio_scenarios import risk_snapshot
    snapshot = risk_snapshot([{"symbol": "512000", "quantity": 100}], {"512000": "1.408"})
    assert snapshot["securities_value"] == "140.80"
    ledger = empty_ledger("100")
    ledger["positions"] = {"512000": {"quantity": 100, "last_buy_date": "2026-09-01"}}
    assert mark_ledger(ledger, trade_date="2026-09-02", prices={"512000": "1.408"})["marks"][-1]["net_asset_value"] == "240.80"


def evaluation_row():
    return dict(symbol="600001", label_start="2026-09-01", label_end="2026-09-10",
                gross_return_pct=5, cost_pct=.2, baseline_return_pct=1, data_class="strict_observed_pit",
                input_receipt_id="receipt", execution_fact_ids=["open", "close"],
                input_observed_at="2026-09-01T09:00:00+08:00", decision_at="2026-09-01T09:20:00+08:00",
                execution_at="2026-09-01T09:30:00+08:00")


@pytest.mark.parametrize("field,value", [("cost_pct", -1), ("gross_return_pct", float("nan")),
                                        ("cost_pct", True), ("decision_at", "2026-09-01T10:00:00+08:00")])
def test_evaluation_rejects_impossible_cost_or_execution(field, value):
    row = {**evaluation_row(), field: value}
    with pytest.raises(ValueError):
        evaluate_rows([row])


def test_replay_keeps_execution_and_closing_gaps_separate(store):
    from decimal import Decimal
    from analysis.decision_evaluation import ExecutionRules
    from application.model_portfolios import ModelPortfolios

    dates = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"]
    first, execution, holding_day, latest = dates
    symbol = "600001"
    books = ModelPortfolios(store)
    archive = SettlementEvidence(store)
    conn = store.connect()
    for day in dates:
        conn.execute("INSERT INTO research_trade_calendar VALUES (?,?,?)", (day, "fixture", day))
    ledger = mark_ledger(empty_ledger(), trade_date=first, prices={}, corporate_actions_complete=True)
    conn.execute("INSERT INTO research_model_portfolios VALUES (?,?,?)", ("top5", json.dumps(ledger), first))
    target = {"published_at": first + "T10:00:00+08:00", "recording_mode": "contemporaneous",
              "weights": {symbol: 1.0}, "policy_hash": "selection"}
    conn.execute("INSERT INTO research_model_targets VALUES (?,?,?,?,?,?)",
                 ("target", "top5", "run", "pending", json.dumps(target), first))
    fusion_policy = {"version": "fixture-fusion-policy"}
    store._insert_selection_artifact(conn, "run", "fusion_policy", fusion_policy, {"policy_hash": "selection"})
    conn.commit()
    conn.close()

    def run(day, facts):
        return books.advance(facts, now=day + "T16:30:00+08:00").get("top5")

    assert run(latest, {})["blocker"]["stage"] == "original_session_required"
    fact = dict(trade_date=execution, provider="fixture", adjustment="raw", open=10, close=10.4,
                volume=1000000, limit_up=11, limit_down=9, execution_rules=asdict(ExecutionRules()),
                corporate_actions_complete=True, data_class="reconstructed_history")
    invalid = {**fact, "adjustment": "qfq"}
    assert run(execution, {(symbol, execution): invalid})["blocker"]["missing"] == {symbol: ["adjustment"]}
    missing_close = run(execution, {(symbol, execution): {**fact, "close": None}})
    assert missing_close["blocker"]["stage"] == "close_valuation"

    def acquire(day, wanted):
        assert wanted == {symbol}
        return {(symbol, day): {**fact, "trade_date": day}}

    replay = archive.recover(books, through_day=latest, acquire=acquire)
    assert set(replay) == {execution, holding_day}
    execution_mark = replay[execution]["top5"]
    holding_mark = replay[holding_day]["top5"]
    assert execution_mark["status"] == holding_mark["status"] == "verified"
    assert holding_mark["data_class"] == "reconstructed_history"
    from application.results import payload_hash
    assert holding_mark["policy_hash"] == "selection"
    assert holding_mark["fusion_policy_hash"] == payload_hash(fusion_policy)
    assert Decimal(execution_mark["fees_paid"]) > 0
    assert execution_mark["fees_paid"] == holding_mark["fees_paid"]
    assert Decimal(execution_mark["net_asset_value"]) == (
        Decimal(execution_mark["cash"]) + Decimal("10.4") * execution_mark["positions"][symbol]["quantity"])
    assert run(holding_day, {}) is None
    conn = store.connect()
    try:
        state, payload = conn.execute("SELECT state,payload FROM research_model_targets").fetchone()
        assert state == "processed"
        assert len(json.loads(payload)["fills"]) == 1
        next_symbol = "600002"
        rebalance = {**target, "published_at": holding_day + "T10:00:00+08:00",
                     "weights": {next_symbol: 1.0}}
        conn.execute("INSERT INTO research_model_targets VALUES (?,?,?,?,?,?)",
                     ("rebalance", "top5", "next", "pending", json.dumps(rebalance), holding_day))
        conn.commit()
    finally:
        conn.close()
    archive.record(symbol, latest, {"trade_date": latest, "provider": "fixture",
                                    "adjustment": "raw", "close": 10.4})

    def acquire_rebalance(day, wanted):
        assert day == latest and wanted == {symbol, next_symbol}
        return {(s, day): {**fact, "trade_date": day} for s in wanted}

    rebalance_mark = archive.recover(books, through_day="2026-09-11", acquire=acquire_rebalance)[latest]["top5"]
    assert rebalance_mark["positions"][symbol]["quantity"] == 0
    assert rebalance_mark["positions"][next_symbol]["quantity"] > 0


def test_historical_zzshare_requires_original_date_and_explicit_pause(monkeypatch):
    import pandas as pd
    from analysis.decision_evaluation import missing_execution_fields
    from data.sources import zzshare
    from jobs.decision_loop_jobs import _execution_fact

    day = "2026-09-08"
    row = dict(ts_code="600001.SH", trade_date="20260908", open=10, close=10, pre_close=9.9,
               volume=100000, high_limit=11, low_limit=9, is_paused=0)
    def daily(trade_date, *, adjust):
        assert trade_date == day and adjust == "raw"
        return pd.DataFrame([row,
                             {**row, "ts_code": "600002.SH", "open": 0, "volume": 0, "is_paused": 1},
                             {**row, "ts_code": "600003.SH", "trade_date": "20260909"},
                             {**row, "ts_code": "600004.SH", "open": 0, "volume": 0}])
    monkeypatch.setattr(zzshare, "get_market_daily", daily)
    facts, detail = zzshare.historical_execution_session(day, {"600001", "600002", "600003", "600004"})
    assert not missing_execution_fields(facts["600001"], day)
    assert facts["600001"]["volume"] == 100000
    assert facts["600002"]["suspended"] and not missing_execution_fields(facts["600002"], day)
    assert "600003" not in facts
    assert detail["wrong_session_symbols"] == ["600003"]
    assert missing_execution_fields(facts["600004"], day) == ["open", "volume"]
    live, gaps = _execution_fact("600002", day, {"open": 0, "price": 10, "volume": 0,
                                                "quote_time": day + "T15:00:00+08:00"}, "fixture")
    assert live["suspended"] and not gaps


def test_historical_collection_completes_existing_execution_work(store, monkeypatch):
    from analysis.decision_evaluation import ExecutionRules
    from data.sources import zzshare
    from jobs.decision_loop_jobs import _historical_acquirer

    archive = SettlementEvidence(store)
    day = "2026-09-08"
    archive.request(["600001"], day)
    fact = dict(trade_date=day, provider="zzshare", adjustment="raw", open=10, close=10.3,
                volume=1000000, limit_up=11, limit_down=9, execution_rules=asdict(ExecutionRules()),
                data_class="reconstructed_history")
    monkeypatch.setattr(zzshare, "available", lambda: True)
    monkeypatch.setattr(zzshare, "historical_execution_session",
                        lambda day, symbols: ({"600001": fact}, {"provider": "zzshare"}))
    acquire = _historical_acquirer(archive)
    recorded = acquire(day, {"600001"})
    assert recorded[("600001", day)]["revision"] == 1
    assert archive.repo.work_status("execution") == {"complete": 1}
    assert archive.repo.work_status("historical_execution") == {"complete": 1}
    assert acquire(day, {"600001"}) == {}


def test_model_period_does_not_publish_initial_zero_or_drawdown_across_gap():
    from analysis.model_ledger import period_performance

    calendar = ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04"]
    marks = [{"trade_date": day, "net_asset_value": nav, "status": "verified"}
             for day, nav in zip(calendar, ["100", "110", "90", "105"])]
    summary = lambda values: period_performance(values, calendar, month="2026-11", through_day=calendar[-1])
    assert summary(marks[:1])["net_return_pct"] is None
    assert summary([marks[0], marks[-1]])["max_drawdown_pct"] is None
    assert summary([marks[0], marks[-1]])["net_return_pct"] == pytest.approx(5)
    assert summary(marks)["max_drawdown_pct"] == pytest.approx(-100 * 20 / 110)


def test_trial_cannot_promote_by_bypassing_registered_protocol(store):
    service = DecisionLoopService(store)
    trial = service.register_trial(hypothesis_id="trend_exhaustion", ast={"op": "field", "name": "close"},
        dataset_id="frozen", data_class="strict_observed_pit", code_revision="revision")
    with pytest.raises(ValueError, match="trial_binding"):
        service.finish_trial(trial["trial_id"], observations=[evaluation_row()])
    assert trial["validation_protocol"]["metric"] == trial["metric"]


def test_same_scores_on_new_dates_are_new_health_evidence():
    from analysis.research_governance import evidence_summary
    first = {"status": "matured", "data_class": "strict_observed_pit", "strategy_version": "v",
             "symbol": "600001", "label_start": "2026-09-01", "label_end": "2026-09-02", "net_excess_return_pct": 1}
    second = {**first, "label_start": "2026-09-03", "label_end": "2026-09-04"}
    assert evidence_summary([first])["evidence_snapshot_id"] != evidence_summary([second])["evidence_snapshot_id"]


def test_archived_recovery_includes_independent_cohorts(store):
    from analysis.decision_evaluation import ExecutionRules
    archive = SettlementEvidence(store)
    order = {"earliest_execution_at": "2026-09-01T09:30:00+08:00", "recording_mode": "contemporaneous"}
    conn = store.connect()
    conn.execute("INSERT INTO research_model_orders VALUES (?,?,?,?,?,?,?,?)", ("o", "r", "fusion", "600001", "pending", json.dumps(order), "t", "t"))
    conn.commit()
    conn.close()
    archive.record("600001", "2026-09-01", {"trade_date": "2026-09-01", "provider": "fixture", "adjustment": "raw",
        "open": 10, "close": 10, "execution_rules": asdict(ExecutionRules())})
    class Books:
        def symbols(self, *, include_targets): return []
        def advance(self, facts, now, *, recorded_at=None): return {"fusion": {"status": "indicative"}}
    class Cohorts:
        def settle_models(self, facts, now):
            assert ("600001", "2026-09-01") in facts
            return {"filled": 1}
    result = archive.recover(Books(), through_day="2026-09-03", cohorts=Cohorts())
    assert result["2026-09-01"]["cohorts_settled"] == {"filled": 1}


def test_new_human_routes_are_admin_only():
    from webui.access_control import ROUTE_POLICIES, Access, MACHINE_CAPABILITIES
    for method, path in [("POST", "/api/research/cases/acknowledge"), ("GET", "/api/portfolio/account-facts")]:
        assert ROUTE_POLICIES[(method, path)] == Access.ADMIN
        assert (method, path) not in MACHINE_CAPABILITIES


def test_browser_domain_errors_are_not_server_errors():
    from webui.decision_loop_routes import browser_errors
    from fastapi import HTTPException
    @browser_errors
    def action(): raise ValueError("revision_conflict")
    with pytest.raises(HTTPException) as result:
        action()
    assert result.value.status_code == 409


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-inf", float("nan")])
def test_trade_import_numbers_are_finite(value):
    from portfolio.trade_import_service import _number
    assert _number(value) is None


def test_interrupted_holdout_has_terminal_audit_not_reopening(store):
    service = DecisionLoopService(store)
    conn = store.connect()
    conn.execute("INSERT INTO research_holdout_batches VALUES ('b','2026-10-01','2026-11-01','evaluating','t','2026-12-01')")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="owner_mismatch"):
        service.void_holdout("b", "another", reason="interrupted")
    result = service.void_holdout("b", "t", reason="interrupted")
    assert not result["promotion_ready"]
    assert service.void_holdout("b", "t", reason="retried") == result
    with pytest.raises(ValueError, match="overlapping"):
        service.seal_holdout(start_date="2026-10-01", end_date="2026-11-01", now="2026-09-01")


def test_missing_old_cohorts_do_not_starve_ready_new_cohort(store):
    from analysis.decision_evaluation import ExecutionRules
    from application.decision_loop import _encode
    service = DecisionLoopService(store)
    conn = store.connect()
    order = {"symbol": "600001", "side": "buy", "cash_budget": "10000", "recording_mode": "contemporaneous",
             "published_at": "2026-08-01T09:00:00+08:00", "earliest_execution_at": "2026-08-02T09:30:00+08:00"}
    for i in range(505):
        conn.execute("INSERT INTO research_model_orders VALUES (?,?,?,?,?,?,?,?)", (str(i), "r", "fusion", "600001", "pending", _encode(order), "a", "a"))
    ready = {**order, "earliest_execution_at": "2026-09-01T09:30:00+08:00"}
    conn.execute("INSERT INTO research_model_orders VALUES (?,?,?,?,?,?,?,?)", ("new", "r", "fusion", "600001", "pending", _encode(ready), "z", "z"))
    conn.commit()
    conn.close()
    facts = {("600001", "2026-09-01"): dict(trade_date="2026-09-01", adjustment="raw", open=10, close=10,
                volume=100000, limit_up=11, limit_down=9, execution_rules=asdict(ExecutionRules()))}
    result = service.settle_models(facts, now="2026-09-01T16:00:00+08:00")
    assert sum(result.values()) == 1


def test_daily_fallback_and_optional_failure_do_not_block_settlement(store, monkeypatch):
    import jobs.decision_loop_jobs as jobs
    import application.model_portfolios as books
    import application.reliability_jobs as optional
    import data.sources.tencent as primary
    import data.datahub as routed
    import application.decision_loop as loop
    from zoneinfo import ZoneInfo
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return datetime(2026, 9, 4, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(jobs, "datetime", Clock)
    monkeypatch.setattr("data.reliability_store.utcnow", lambda: "2026-09-04T10:00:01+00:00")
    archive = SettlementEvidence(store)
    archive.request([f"60{i:04d}" for i in range(160)], "2026-09-03")
    # Avoid dependence on the wall-clock close while still testing the wiring.
    import data.valuation_contract as contract
    monkeypatch.setattr(contract, "closing_timestamp", lambda value, day: day + "T15:00:00+08:00")
    calls = []
    class Service:
        def __init__(self, given): self.store = given
        def capsule(self): return None
        def settle_models(self, facts, now):
            assert ("600001", "2026-09-04") in facts
            calls.append("settled")
            return {"filled": 1}
    class Books:
        def __init__(self, given): pass
        def symbols(self, *, include_targets): return ["600001"]
        def advance(self, facts, now, *, recorded_at=None): return {"fusion": {"status": "indicative"}}
    monkeypatch.setattr(loop, "DecisionLoopService", Service)
    monkeypatch.setattr(books, "ModelPortfolios", Books)
    monkeypatch.setattr(primary, "quotes", lambda batch: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(routed, "quotes", lambda batch: {"600001": dict(open=10, price=10, volume=100000, limit_up=11, limit_down=9, quote_time="2026-09-04T15:00:00+08:00")})
    monkeypatch.setattr(optional, "refresh_corporate_evidence", lambda *a, **kw: {})
    replay_limits = []
    def failed_reliability(*args, **kwargs):
        replay_limits.append(kwargs.get("replay_limit"))
        raise ValueError()
    monkeypatch.setattr(optional, "refresh_reliability", failed_reliability)
    monkeypatch.setattr(jobs, "refresh_quality", lambda *a: {})
    result = jobs.daily_decision_loop(store)
    assert calls == ["settled"] and result["status"] == "partial"
    assert replay_limits == [0]
    assert {e["component"] for e in result["errors"]} == {"tencent", "research_reviews"}
    assert result["execution_fact_count"] == 1
    assert archive.repo.work_status("execution") == {"pending": 160, "complete": 1}


def test_eod_model_metrics_are_shared_by_task_list_and_close_review():
    from application.scheduled_snapshot import _job_run
    from jobs.task_health import format_eod_models, scheduled_outcome

    legacy = {"job_name": "eod_outcomes", "status": "success", "error": "decision_loop=partial"}
    assert scheduled_outcome(legacy)["status"] == "partial"
    detail = format_eod_models({
        "status": "partial", "requested_count": 15, "execution_fact_count": 12,
        "settled": {"filled": 4}, "oldest_pending_date": "2026-11-25", "waiting_books": 2,
        "pending_reason": "original_execution_or_mark_facts_missing",
        "model_books": {"top5": {"blocker": {"stage": "execution_evidence", "trade_date": "2026-11-25",
                                             "target_id": "a", "missing": {"600001": ["open"]}}}},
        "recovered_original_sessions": {"2026-11-24": {"cohorts_settled": {"filled": 3}}},
    })
    run = {**legacy, "status": "partial", "error": detail, "finished_at": "2026-11-26T19:15:00+08:00"}
    metrics = scheduled_outcome(run)["metrics"]
    assert metrics["model_settled"] == 7
    assert metrics["model_observed"] == 12 and metrics["model_requested"] == 15
    assert metrics["model_oldest_pending"] == "2026-11-25"
    assert metrics["model_book_blockers"]["top5"]["missing"] == {"600001": ["open"]}
    assert _job_run(run)["metrics"] == metrics
