import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from data.research_store import ResearchStore
from data.reliability_store import ReliabilityStore
from data.acquisition_evidence import source_family, compatible_fact, archive_raw, revision_impact
from data.corporate_actions import normalize_dividends, coverage_complete
from analysis.model_ledger import empty_ledger, apply_corporate_action, mark_ledger
from analysis.decision_evaluation import ExecutionRules
from analysis.execution_scenarios import execution_scenarios
from analysis.portfolio_scenarios import non_trade_band, risk_snapshot, stress
from application.research_cases import ResearchCases, compile_claim, validity
from application.account_reconciliation import AccountReconciliation
from application.strategy_health import transition
from application.decision_loop import DecisionLoopService
from application.settlement_evidence import SettlementEvidence
from agent.process_evals import development_cases, run_case


@pytest.fixture
def store(tmp_path):
    return ResearchStore(str(tmp_path / "test.db"), connect_fn=sqlite3.connect)


def test_append_only_owner_cas(store):
    repo = ReliabilityStore(store)
    repo.put("case", "x", {"text": "one"}, owner="a")
    assert repo.get("case", "x", "b") is None
    with pytest.raises(ValueError, match="revision_conflict"):
        repo.put("case", "x", {"text": "bad"}, owner="a")
    repo.put("case", "x", {"text": "two"}, owner="a", expected_revision=1)
    assert repo.list("case", owner="a")[0]["text"] == "two"
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM research_reliability_records").fetchone()[0] == 2
    conn.close()


def test_queue_fairness_and_budget(store):
    repo = ReliabilityStore(store)
    for i in range(170):
        repo.enqueue("x", str(i), {"index": i}, available_at="2026-01-01T00:00:00+00:00")
    first = repo.claim("x", now="2026-01-01T01:00:00+00:00")
    second = repo.claim("x", now="2026-01-01T01:01:00+00:00")
    assert len(first) == 160 and len(second) == 10
    assert not {r["work_id"] for r in first} & {r["work_id"] for r in second}
    for r in first:
        repo.finish(r["work_id"])
    assert repo.work_status("x") == {"complete": 160, "pending": 10}


def test_queue_timezone_and_crash_backoff(store):
    repo = ReliabilityStore(store)
    repo.enqueue("x", "a", {}, available_at="2026-09-04T19:00:00+08:00")
    assert repo.claim("x", now="2026-09-04T10:59:00+00:00") == []
    assert len(repo.claim("x", now="2026-09-04T11:00:00+00:00")) == 1
    assert repo.claim("x", now="2026-09-04T19:01:00+08:00") == []


def test_source_family_and_semantics():
    assert source_family("mootdx") == source_family("eltdx")
    assert source_family("akshare") == "unknown"
    row = dict(symbol="600001", field="pe_ttm", unit="ratio", currency="CNY", period="TTM", basis="consolidated", effective_date="2026-09-01")
    assert compatible_fact(row, row)
    for key in row:
        assert not compatible_fact(row, {**row, key: None})


def test_private_archive(tmp_path):
    assert not archive_raw(b"x", root=tmp_path / "not-created")["raw_replayable"]
    assert not (tmp_path / "not-created").exists()
    first = archive_raw(b"public facts", root=tmp_path / "raw", retention_allowed=True)
    assert archive_raw(b"public facts", root=tmp_path / "raw", retention_allowed=True) == first
    assert (tmp_path / "raw" / first["locator"]).stat().st_mode & 0o077 == 0


def test_dividend_receivable_and_payment():
    ledger = empty_ledger("100")
    event = dict(event_id="accrual", kind="dividend_accrual", symbol="600001", price_basis="raw",
                 confirmed=True, entitled_quantity=100, entitlement_evidence="record-date-ledger", net_cash_per_share="0.1", payment_date="2026-09-10")
    accrued = apply_corporate_action(ledger, event)
    assert accrued["cash"] == "100.00"
    assert apply_corporate_action(accrued, event) == accrued
    marked = mark_ledger(accrued, trade_date="2026-09-09", prices={}, corporate_actions_complete=True)
    assert marked["marks"][-1]["net_asset_value"] == "110.00"
    payment = dict(event_id="paid", kind="dividend_payment", accrual_id="accrual", amount="10", price_basis="raw", confirmed=True, effective_date="2026-09-10")
    assert apply_corporate_action(accrued, payment)["cash"] == "110.00"
    with pytest.raises(ValueError, match="not_due"):
        apply_corporate_action(accrued, {**payment, "effective_date": "2026-09-09"})


def test_dividends_not_all_corporate_actions():
    rows = [dict(ex_date="20260904", div_proc="实施", cash_div=.1, pay_date="20260910")]
    value = normalize_dividends(rows, "600001", start="2026-09-01", end="2026-09-04", observed_at="2026-09-04")
    assert len(value["events"]) == 1 and value["gaps"]
    assert not coverage_complete(value, symbol="600001", day="2026-09-04", events=[])


def claim():
    return {"text": "增长 20%", "passages": [{"source_id": "filing", "quote": "本期120，上期100",
        "published_at": "2026-08-30T00:00:00+08:00", "first_seen_at": "2026-08-30T12:00:00+08:00", "locator": "p1",
        "unit": "CNY_100M", "currency": "CNY", "basis": "consolidated", "period_kind": "annual"}],
        "calculation": {"operation": "growth_pct", "operands": [{"value": "120", "passage_index": 0}, {"value": "100", "passage_index": 0}]},
        "expires_at": "2026-10-01T00:00:00+08:00"}


def test_claim_recalculation_and_expiry():
    value = compile_claim(**claim())
    assert Decimal(value["computed_value"]) == 20
    assert validity([value], now="2026-10-02T00:00:00+08:00")["use"] == "review_required"
    bad = claim()
    bad["calculation"]["expected"] = 30
    with pytest.raises(ValueError, match="number_mismatch"):
        compile_claim(**bad)


def test_thesis_lock_owner_and_immutable_history(store):
    cases = ResearchCases(store)
    draft = cases.draft("600001", owner="user", text="观察", claims=[claim()])
    with pytest.raises(PermissionError):
        cases.lock("600001", owner="user", draft_revision=1)
    lock = cases.lock("600001", owner="user", draft_revision=1, human_confirmed=True)
    cases.draft("600001", owner="user", text="改草稿", claims=[], expected_revision=draft["revision"])
    assert cases.repo.get("thesis_lock", "600001", "user")["text"] == lock["text"]
    assert not cases.view(owner="other")["theses"]


def test_major_unknown_event_not_blocked_by_no_thesis(store):
    cases = ResearchCases(store)
    value = cases.event("600001", {"event_id": "e1", "source_id": "filing", "published_at": "2026-09-04", "severity": "unknown"})
    assert value["review"] == "urgent"
    assert cases.view()["no_thesis_blocks_trades"] is False
    assert cases.repo.work_status("case_review")["pending"] == 1


def test_actual_investigation_admission_and_cached_tools(store):
    cases = ResearchCases(store)
    cases.event("600001", {"event_id": "e", "source_id": "filing", "published_at": "2026-09-04"})
    called = []
    def call(*args, **kwargs):
        called.append(1)
        return '{"summary":"缺少估值，需要核实","missing":["valuation"]}', 'fixture:model'
    row = {"work_id": "one", "symbol": "600001", "event_id": "e"}
    value = cases.investigate(row, now="2026-09-04T19:00:00+08:00", call=call)
    assert value["status"] == "draft" and value["tool_calls"] == 3
    cases.investigate(row, now="2026-09-04T19:00:00+08:00", call=call)
    assert len(called) == 1


def test_forecast_unknown_void_and_scoring(store):
    cases = ResearchCases(store)
    now = "2026-09-04T10:00:00+08:00"
    with pytest.raises(ValueError):
        cases.predict("600001", owner="a", probability=.8, target_date="2026-09-01", benchmark="positive_net_excess", evidence_id="e", now=now)
    value = cases.predict("600001", owner="a", probability=.8, target_date="2026-09-10", benchmark="positive_net_excess", evidence_id="e", now=now)
    unknown = cases.adjudicate(value["object_id"], owner="a", now="2026-09-11")
    assert unknown["status"] == "pending" and unknown["brier"] is None
    result = cases.adjudicate(value["object_id"], owner="a", outcome=True, evidence_id="settlement", now="2026-09-11")
    assert result["brier"] == pytest.approx(.04)
    assert cases.calibration(owner="a")["unresolved_fraction"] == 0


def test_account_import_preview_confirm_and_conflicts(store):
    service = AccountReconciliation(store)
    rows = [dict(kind="deposit", external_id="deposit1", date="2026-09-02", amount="100")]
    preview = service.preview(rows, owner="a", watermark="trades-v1")
    assert not service.repo.list("account_fact", owner="a")
    confirmed = service.confirm(preview["object_id"], owner="a", watermark="trades-v1")
    assert service.confirm(preview["object_id"], owner="a", watermark="trades-v1") == confirmed
    second = service.preview([dict(kind="fee", external_id="fee1", date="2026-09-02", amount="5")], owner="a", watermark="trades-v1")
    service.confirm(second["object_id"], owner="a", watermark="trades-v1")
    result = service.reconcile(owner="a", opening=1000, closing=1105, securities_pnl=10, start="2026-09-01", end="2026-09-03")
    assert result["unexplained"] == "0.00" and result["twr"] is None


def test_holdout_requires_real_evaluation_and_reserves_before_view(store):
    loop = DecisionLoopService(store)
    trial = loop.register_trial(hypothesis_id="trend_exhaustion", ast={"op": "field", "name": "close"}, dataset_id="x", data_class="strict_observed_pit", code_revision="test")
    loop.finish_trial(trial["trial_id"])
    conn = store.connect()
    conn.execute("INSERT INTO research_holdout_batches VALUES ('b','2026-01-01','2026-02-01','sealed',NULL,NULL)")
    conn.commit()
    conn.close()
    def evaluator(batch, trial):
        conn = store.connect()
        assert conn.execute("SELECT state FROM research_holdout_batches WHERE batch_id='b'").fetchone()[0] == "evaluating"
        conn.close()
        return [dict(symbol="600001", label_start="2026-01-01", label_end="2026-01-10", gross_return_pct=5, cost_pct=.2,
                     baseline_return_pct=1, strategy_version="test", data_class="strict_observed_pit",
                     input_receipt_id="fixture", execution_fact_ids=["fixture"],
                     input_observed_at="2025-12-31T15:00:00+08:00", decision_at="2026-01-01T09:00:00+08:00")]
    result = loop.consume_holdout("b", trial["trial_id"], evaluator=evaluator)
    assert result["evaluation"]["row_count"] == 1
    assert not result["evaluation"]["promotion_ready"]
    with pytest.raises(ValueError, match="already_consumed"):
        loop.consume_holdout("b", trial["trial_id"], evaluator=evaluator)


def test_health_hysteresis():
    bad = dict(status="sufficient", conservative_lower_bound_pct=-1, promotion_ready=False)
    first = transition(None, bad, now="2026-09-01T00:00:00+00:00")
    assert first["state"] == "active"
    second = transition(first, bad, now="2026-09-08T00:00:00+00:00")
    assert second["state"] == "cooling"
    assert transition(None, {"status": "insufficient_evidence"}, now="2026-09-01")["state"] == "active"


def test_real_lane_ablation_refills_from_core():
    import pandas as pd
    from analysis.local_fusion import LocalFusionComposer
    core = [dict(symbol=f"600{i:03}", total_score=90 - i, industry=f"industry{i}", data_coverage=1.) for i in range(20)]
    local = {"strategies": {"低估值": {"status": "ready", "rows": [dict(symbol="000001", lane_score=95, industry="extra")]}}}
    eligible = pd.DataFrame(core + [dict(symbol="000001", total_score=95, industry="extra", data_coverage=1.)])
    composer = LocalFusionComposer()
    normal = composer.compose(core, local, {"rows": []}, eligible)
    ablated = composer.ablations(core, local, {"rows": []}, eligible)["without_satellite"]["result"]
    assert len(ablated["top15"]) == 15
    assert "000001" not in [r["symbol"] for r in ablated["top15"]]
    assert {r["symbol"] for r in ablated["top15"]} != {r["symbol"] for r in normal["top15"]}


def test_late_runner_recovers_original_day_not_current_price(store):
    from application.model_portfolios import ModelPortfolios
    from application.decision_capsule import build_capsule
    now = datetime.now(timezone.utc)
    published = now - timedelta(minutes=5)
    execution = (now + timedelta(days=1)).date().isoformat()
    later = (now + timedelta(days=2)).date().isoformat()
    capsule = build_capsule(run_id="forward", metadata={"selection_date": published.date().isoformat(), "market_as_of": published.date().isoformat(), "manifest_id": "m", "policy_hash": "p"},
        top15=[{"symbol": "600001", "assigned_lane": "core"}], top5=[{"symbol": "600001"}],
        published_at=published.isoformat(), next_open_date=execution)
    books = ModelPortfolios(store)
    books.publish(capsule, {"fusion": [{"symbol": "600001"}]})
    assert books.advance({}, now=later + "T16:30:00+08:00")["fusion"]["status"] == "waiting_data"
    archive = SettlementEvidence(store)
    archive.record("600001", execution, {"trade_date": execution, "adjustment": "raw", "provider": "fixture",
        "open": 10, "close": 10, "volume": 1000000, "limit_up": 11, "limit_down": 9,
        "execution_rules": asdict(ExecutionRules()), "corporate_actions_complete": True})
    restored = archive.recover(books, through_day=later)
    assert restored[execution]["fusion"]["trade_date"] == execution
    assert restored[execution]["fusion"]["status"] == "indicative"  # boolean is not coverage proof


def test_research_http_and_human_lock_are_separate_authorities():
    from webui.access_control import ROUTE_POLICIES, MACHINE_CAPABILITIES, Access
    assert ROUTE_POLICIES[("POST", "/api/research/thesis/lock")] == Access.ADMIN
    assert ROUTE_POLICIES[("POST", "/api/portfolio/account-facts/confirm")] == Access.ADMIN
    assert MACHINE_CAPABILITIES[("GET", "/api/machine/v1/agent/research/cases")] == "foliant.selection.read"
    assert ("POST", "/api/research/thesis/lock") not in MACHINE_CAPABILITIES


def test_execution_stresses_and_no_trade_band():
    rules = ExecutionRules()
    order = dict(side="buy", quantity=100, published_at="2026-09-01T10:00:00+08:00", earliest_execution_at="2026-09-02T09:30:00+08:00")
    fact = dict(trade_date="2026-09-02", open=10, volume=100000, limit_up=11, limit_down=9, adjustment="raw")
    values = execution_scenarios(order, fact, rules, cash=10000, sellable=100)
    assert len(values) == 6
    assert values[2]["result"]["reason"] == "limit_locked"
    assert values[4]["result"]["status"] == "unavailable"
    assert non_trade_band(price=10, rules=rules, expected_edge_pct=.01, uncertainty_pct=.5)["action"] == "hold"


@pytest.mark.parametrize("case", development_cases(), ids=lambda c: c["id"])
def test_model_process_fixture_contract(case):
    calls = 0
    def call(messages, **kwargs):
        nonlocal calls
        calls += 1
        required = 2 if case.get("transient_tool_failure") else 1
        return (json.dumps({"tool": "facts", "args": {}}) if calls <= required else json.dumps({"final": case["expected"]}), "fixture:model")
    result = run_case(case, call=call)
    assert result["passed"] and result["content_logged"] is False


def test_process_eval_rejects_unauthorized_tool():
    value = run_case(development_cases()[0], call=lambda *a, **k: ('{"tool":"shell"}', "fixture:model"))
    assert not value["passed"] and "permission" in value["failures"]


def test_hard_risk_cannot_be_reclassified_by_learning():
    previous = {"state": "risk_halted", "bad_streak": 2, "good_streak": 3}
    for lower in (-10, 10):
        value = transition(previous, {"status": "sufficient", "conservative_lower_bound_pct": lower,
                           "promotion_ready": lower > 0}, now="2026-09-04T10:00:00+00:00")
        assert value["state"] == "risk_halted"


def test_case_attention_deduplicates_symbols_and_prefers_fresh_risks(store):
    cases = ResearchCases(store)
    for i in range(8):
        cases.event("600001", {"source_id": "fixture", "event_id": str(i), "severity": "major",
                              "published_at": f"2026-09-0{1 + i}T00:00:00+00:00"})
    cases.event("600002", {"source_id": "fixture", "event_id": "other", "severity": "major",
                          "published_at": "2026-09-09T00:00:00+00:00"})
    attention = cases.view()["attention_top5"]
    assert len(attention) == 2
    assert attention[0]["symbol"] == "600002"
    assert attention[1]["event_id"] == "7"


def test_revision_replay_ignores_period_not_in_original_input():
    import pandas as pd
    from application.revision_replay import replay_impact
    class Frozen:
        def load_selection_manifest(self, identity):
            return {"run_id": "old"}
        def load_financial_facts_from_manifest(self, identity):
            return {"income": pd.DataFrame([{"symbol": "600001", "stat_date": "2025-12-31"}])}
    result = replay_impact(Frozen(), impact={"symbol": "600001", "table": "income", "stat_date": "2026-06-30"}, manifest_id="old")
    assert result["reason"] == "period_not_consumed"


def test_missing_older_target_prevents_newer_fills(store):
    from application.model_portfolios import ModelPortfolios
    from application.decision_capsule import build_capsule
    now = datetime.now(timezone.utc)
    publication = (now - timedelta(minutes=5)).isoformat()
    first_day = (now + timedelta(days=1)).date().isoformat()
    second_day = (now + timedelta(days=2)).date().isoformat()
    books = ModelPortfolios(store)
    for name, day in (("old", first_day), ("new", second_day)):
        capsule = build_capsule(run_id=name, metadata={"selection_date": publication[:10],
            "market_as_of": publication[:10], "manifest_id": "m", "policy_hash": "p"},
            top15=[{"symbol": "600001", "assigned_lane": "core"}], top5=[{"symbol": "600001"}],
            published_at=publication, next_open_date=day)
        books.publish(capsule, {"fusion": [{"symbol": "600001"}]})
    fact = dict(trade_date=second_day, adjustment="raw", open=10, close=10,
                volume=1000000, limit_up=11, limit_down=9, execution_rules=asdict(ExecutionRules()))
    assert books.advance({("600001", second_day): fact}, now=second_day + "T16:30:00+08:00")["fusion"]["status"] == "waiting_data"
    conn = store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM research_model_targets WHERE state='processed'").fetchone()[0] == 0
        book = json.loads(conn.execute("SELECT payload FROM research_model_portfolios WHERE baseline='fusion'").fetchone()[0])
        assert not book["positions"] and not book["marks"]
    finally:
        conn.close()
