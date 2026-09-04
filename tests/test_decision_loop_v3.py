import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from analysis.account_action_plan import build_action_plan, model_portfolio, plan_valid
from analysis.decision_evaluation import ExecutionRules, price_metrics, reconcile_book, simulate_fill
from analysis.factor_ast import evaluate, fingerprint
from analysis.local_fusion import FusionPolicy
from analysis.optimizer import aligned_returns
from analysis.research_governance import evidence_summary, purge_training_intervals
from analysis.strategy_policy_controller import validate_proposal
from application.decision_capsule import build_capsule, context_envelope
from application.decision_loop import DecisionLoopService
from application.results import payload_hash
from data.research_store import ResearchStore


@pytest.fixture(autouse=True)
def fixed_forward_recording_clock(monkeypatch):
    from zoneinfo import ZoneInfo
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 9, 4, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            return value.astimezone(tz) if tz else value
    monkeypatch.setattr("application.decision_loop.datetime", Clock)
    monkeypatch.setattr("application.model_portfolios.datetime", Clock)


@pytest.fixture
def store(tmp_path):
    return ResearchStore(str(tmp_path / "research.db"), connect_fn=sqlite3.connect)


@pytest.fixture
def capsule():
    return build_capsule(run_id="r1", metadata={"selection_date": "2026-09-04",
                         "market_as_of": "2026-09-03", "manifest_id": "m1", "policy_hash": "p1"},
                         top15=[{"symbol": "600001", "assigned_lane": "core"}],
                         top5=[{"symbol": "600001", "industry": "bank", "themes": ["finance"]}],
                         published_at="2026-09-04T09:47:00+08:00", next_open_date="2026-09-07")


@pytest.mark.parametrize("changes", [
    [{"path": "strategy_priority.主力资金", "from": 1., "to": .9},
     {"path": "strategy_priority.主力资金", "from": .9, "to": .8}],
    [{"path": "top15_timing_cap", "from": 2, "to": True}],
    [{"path": "top15_timing_cap", "from": 2, "to": 1.5}],
    [{"path": "top15_timing_cap", "from": 2, "to": float("nan")}],
    [{"path": "genome_min_lane_score", "from": 45., "to": 40.}],
])
def test_policy_strict_inputs_and_risk_direction(changes):
    current = FusionPolicy().as_dict()
    original = json.dumps(current, sort_keys=True)
    result = validate_proposal({"base_policy_hash": "p1", "evidence_snapshot_id": "e1", "changes": changes},
                               current, {"evidence_snapshot_id": "e1", "strategies": []}, "p1")
    assert not result[0]
    assert json.dumps(current, sort_keys=True) == original


def test_return_meanings():
    value = price_metrics(100, [100, 120, 110], [100, 110, 105])
    assert value["mae_pct"] == 0
    assert value["close_max_drawdown_pct"] == pytest.approx(-8.333333)
    assert not value["executable_round_trip"]


def test_calendar_aligned_return_intervals():
    dates = pd.date_range("2026-01-01", periods=40)
    a = pd.DataFrame({"close": np.arange(40) + 100}, index=dates)
    b = a.drop(dates[20])
    matrix, codes = aligned_returns({"a": a, "b": b})
    assert codes == ["a", "b"]
    assert len(matrix) == 37  # two intervals around suspension are not interchangeable
    assert np.allclose(matrix[:, 0], matrix[:, 1])


def _order():
    return {"side": "buy", "quantity": 200, "published_at": "2026-09-04T09:45:00+08:00",
            "earliest_execution_at": "2026-09-07T09:30:00+08:00"}


def _bar():
    return {"trade_date": "2026-09-07", "open": 10, "close": 10.3, "volume": 100000,
            "limit_up": 11, "limit_down": 9, "adjustment": "raw"}


def test_fills_after_publication_never_close_fallback():
    bar = _bar()
    assert simulate_fill(_order(), bar, ExecutionRules(), cash=3000)["status"] == "filled"
    bar["open"] = None
    assert simulate_fill(_order(), bar, ExecutionRules(), cash=3000)["status"] == "unfilled"
    bar = {**_bar(), "trade_date": "2026-09-04"}
    assert simulate_fill(_order(), bar, ExecutionRules(), cash=3000)["reason"] == "before_publication"


def test_partial_and_locked_fills():
    result = simulate_fill(_order(), {**_bar(), "volume": 10000}, ExecutionRules(), cash=3000)
    assert result["status"] == "partially_filled" and result["quantity"] == 100
    assert simulate_fill(_order(), {**_bar(), "open": 11}, ExecutionRules(), cash=3000)["reason"] == "limit_locked"
    assert simulate_fill(_order(), _bar(), ExecutionRules(min_buy=200, buy_step=1), cash=1500)["status"] == "unfilled"


def test_reconciliation_account_boundary():
    events = [{"event_id": "e1", "kind": "buy", "cash_delta": "-1005"}]
    full = reconcile_book(opening_cash=2000, closing_cash=995, opening_market_value=0,
                          closing_market_value=1020, events=events)
    assert full["pnl"] == "15.00" and full["external_flow"] == "0.00"
    security = reconcile_book(opening_cash=2000, closing_cash=995, opening_market_value=0,
                              closing_market_value=1020, events=events, scope="securities_only")
    assert full["pnl"] == security["pnl"]
    with pytest.raises(ValueError):
        reconcile_book(opening_cash=0, closing_cash=0, opening_market_value=0,
                       closing_market_value=0, events=events * 2)


def test_capsule_immutable_scoped(capsule):
    original = payload_hash(capsule)
    value = context_envelope(capsule, now="2026-09-05T10:00:00+08:00", quotes={"a": 12})
    value["dynamic"]["quotes"]["a"] = 13
    assert payload_hash(capsule) == original
    with pytest.raises(PermissionError):
        context_envelope(capsule, now="today", holdings_version="private")
    assert model_portfolio(capsule)["cash_weight"] == .8


def test_high_exposure_no_qualified_buy(capsule):
    now = "2026-09-04T10:05:00+08:00"
    quote = {"price": 10, "observed_at": now}
    plan = build_action_plan(capsule, [{"symbol": "600001", "quantity": 1000}],
                             {"600001": quote}, holdings_version="h1", now=now,
                             cash=100, allow_add=True, owner_id="owner")
    assert "暂不操作" in plan["summary"]
    assert not plan["alternatives"][2]["actions"]
    assert not plan_valid(plan, holdings_version="h2", quotes={"600001": quote}, now=now, owner_id="owner")
    with pytest.raises(PermissionError):
        plan_valid(plan, holdings_version="h1", quotes={}, now=now, owner_id="researcher")


def test_evidence_does_not_count_repeated_nominations():
    rows = [{"status": "matured", "data_class": "strict_observed_pit", "strategy_version": "v1",
             "symbol": str(i), "label_start": "2026-07-01", "label_end": "2026-07-10",
             "net_excess_return_pct": 1.} for i in range(100)]
    value = evidence_summary(rows)
    assert value["effective_samples"] == 1 and not value["promotion_ready"]
    assert not evidence_summary([{**r, "data_class": "exploratory"} for r in rows])["promotion_ready"]


def test_purging_label_overlap():
    training = [{"label_start": "2026-07-01", "label_end": "2026-07-20"},
                {"label_start": "2026-06-01", "label_end": "2026-06-10"}]
    test = [{"label_start": "2026-07-15", "label_end": "2026-07-25"}]
    assert purge_training_intervals(training, test) == training[1:]


def test_ast_is_not_python_and_canonicalizes_search():
    a, b = {"op": "field", "name": "close"}, {"op": "constant", "value": 2}
    assert fingerprint({"op": "add", "args": [a, b]}, ["close"]) == fingerprint({"op": "add", "args": [b, a]}, ["close"])
    assert evaluate({"op": "lag", "window": 1, "args": [a]}, {"close": [10, 11, 12]}) == [None, 10, 11]
    for node in ({"op": "__import__", "name": "os"}, {"op": "field", "name": "__secret__"},
                 {"op": "constant", "value": True}, {"op": "lag", "window": -1, "args": [a]}):
        with pytest.raises(ValueError):
            evaluate(node, {"close": [10]})


def test_trial_registry_failure_duplicate_and_holdout(store):
    service = DecisionLoopService(store)
    args = dict(hypothesis_id="trend_exhaustion", ast={"op": "field", "name": "close"},
                dataset_id="d1", data_class="exploratory", code_revision="c1")
    trial = service.register_trial(**args, now="2026-09-04T10:00:00+08:00")
    duplicate = service.register_trial(**args, now="2026-09-04T10:01:00+08:00")
    assert duplicate["state"] == "duplicate"
    finished = service.finish_trial(trial["trial_id"], observations=[])
    assert not finished["evidence"]["promotion_ready"]
    conn = store.connect()
    conn.execute("INSERT INTO research_holdout_batches VALUES ('b1','2026-01-01','2026-03-01','sealed',NULL,NULL)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="evaluator_required"):
        service.consume_holdout("b1", trial["trial_id"])
    assert service.consume_holdout("b1", trial["trial_id"], evaluator=lambda batch, trial: [])["state"] == "retired"
    with pytest.raises(ValueError):
        service.consume_holdout("b1", trial["trial_id"], evaluator=lambda batch, trial: [])


def test_forward_cohorts_idempotent_and_missing_data_expires(store, capsule):
    service = DecisionLoopService(store)
    assert service.start_model_cohorts(capsule)["created"] > 0
    assert service.start_model_cohorts(capsule)["created"] == 0
    assert not service.settle_models({}, now="2026-09-07T18:00:00+08:00")
    counts = service.settle_models({}, now="2026-09-15T18:00:00+08:00")
    assert counts["expired"] > 0


def test_policy_publication_compare_and_swap(store):
    policy = FusionPolicy().as_dict()
    store.save_selection_strategy_records("r1", [], policy=policy, policy_hash="base", selection_date="2026-09-04")
    proposal = {"proposal_id": "p1", "base_policy_hash": "base", "evidence_snapshot_id": "e1",
                "effective_from": "2099-01-01T09:00:00+08:00"}
    updated = {**policy, "top15_satellite_cap": 4}
    store.save_strategy_policy_proposal(proposal, validation_status="applied", applied_policy=updated)
    conn = store.connect()
    saved_hash = conn.execute("SELECT policy_hash FROM strategy_policy_versions WHERE state='scheduled'").fetchone()[0]
    conn.close()
    assert saved_hash == FusionPolicy(**updated).policy_hash
    assert store.save_strategy_policy_proposal(proposal, validation_status="applied", applied_policy=updated) == "p1"
    assert store.load_active_strategy_policy()["policy_hash"] == "base"
    with pytest.raises(ValueError, match="compare_and_swap"):
        store.save_strategy_policy_proposal({**proposal, "proposal_id": "p2"}, validation_status="applied", applied_policy=policy)


def test_quality_cached_read_never_rescores(store):
    from core.research_health import cached_snapshot
    with patch("analysis.local_stock_selector.LocalStockSelector._score_fundamentals", side_effect=AssertionError("must not score")):
        report = cached_snapshot(store=store, selection_date="2026-09-04")
        assert not report["ready"] and report["status"] == "stale"


def test_provider_budget_fail_closed_and_shared(tmp_path, monkeypatch):
    from data.provider_governor import SourceBudgetUnavailable, provider_slot
    monkeypatch.setenv("FOLIANT_RUNTIME_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_DAILY_LIMIT_TENCENT", "1")
    with provider_slot("tencent"):
        pass
    with pytest.raises(SourceBudgetUnavailable, match="daily_budget"):
        with provider_slot("tencent"):
            pytest.fail("budget bypass")


def test_shared_quota_survives_a_second_process(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from data.provider_governor import provider_slot
    monkeypatch.setenv("FOLIANT_RUNTIME_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_DAILY_LIMIT_TENCENT", "1")
    with provider_slot("tencent"):
        pass
    result = subprocess.run([sys.executable, "-c", "from data.provider_governor import provider_slot;\nwith provider_slot('tencent'): pass"],
                            capture_output=True, text=True, env=dict(os.environ), timeout=10)
    assert result.returncode != 0 and "daily_budget_exhausted" in result.stderr


def test_route_timeout_does_not_requeue_still_running_call():
    import threading
    from data import datahub
    release = threading.Event()
    invoked = []
    def slow():
        invoked.append(1)
        release.wait(3)
        return [1]
    try:
        assert datahub._route("bounded-test", [("slow-test", slow)], timeout=.01) is None
        assert datahub._route("bounded-test", [("slow-test", slow)], timeout=.01) is None
        assert len(invoked) == 1
    finally:
        release.set()


def test_quality_generation_change_invalidates_report(store):
    from core.research_health import cached_snapshot, refresh_quality_report
    with patch.object(store, "generation_vector", return_value={"daily_market": 1}), \
         patch("core.research_health.snapshot", return_value={"status": "ready", "ready": True,
               "checks": {"formal_selection": True, "market_coverage": True}}):
        refresh_quality_report(store=store, selection_date="2026-09-04", mode="preopen")
        assert cached_snapshot(store=store, selection_date="2026-09-04", mode="preopen")["ready"]
    with patch.object(store, "generation_vector", return_value={"daily_market": 2}):
        assert not cached_snapshot(store=store, selection_date="2026-09-04", mode="preopen")["ready"]


def test_model_cash_security_and_corporate_events():
    from analysis.model_ledger import empty_ledger, apply_fill, apply_corporate_action, mark_ledger
    fill = simulate_fill(_order(), _bar(), ExecutionRules(), cash=3000)
    ledger = apply_fill(empty_ledger(3000), event_id="fill1", symbol="600001", side="buy", fill=fill)
    assert apply_fill(ledger, event_id="fill1", symbol="600001", side="buy", fill=fill) == ledger
    event = {"event_id": "div1", "kind": "cash_dividend", "price_basis": "raw", "confirmed": True,
             "entitled_quantity": 100, "net_cash_per_share": ".1"}
    dividend = apply_corporate_action(ledger, event)
    assert Decimal(dividend["cash"]) == Decimal(ledger["cash"]) + 10
    with pytest.raises(ValueError):
        apply_corporate_action(ledger, {**event, "price_basis": "qfq"})
    marked = mark_ledger(dividend, trade_date="2026-09-07", prices={"600001": 10.3})
    assert marked["marks"][-1]["status"] == "indicative"
    assert marked["marks"][-1]["fees_paid"] != "0.00"


def test_continuous_models_and_low_turnover_are_real_accounts(store, capsule):
    from application.model_portfolios import ModelPortfolios
    portfolios = ModelPortfolios(store)
    portfolios.publish(capsule, {"fusion": capsule["opportunity_set"]["top15"]})
    portfolios.publish(capsule, {"fusion": capsule["opportunity_set"]["top15"]})
    facts = {("600001", "2026-09-07"): {**_bar(), "execution_rules": {}, "corporate_actions_complete": True}}
    # Empty dict means default rule data is not sufficiently declared.
    from dataclasses import asdict
    facts[("600001", "2026-09-07")]["execution_rules"] = asdict(ExecutionRules())
    marks = portfolios.advance(facts, now="2026-09-07T18:00:00+08:00")
    assert set(marks) == {"fusion", "low_turnover"}
    assert marks["fusion"]["net_return_pct"] == marks["low_turnover"]["net_return_pct"]
    assert Decimal(marks["fusion"]["fees_paid"]) > 0
    assert portfolios.advance(facts, now="2026-09-07T18:30:00+08:00") == {}


def test_isolated_worker_rejects_unpinned_images_and_has_no_mounts():
    from application.research_lab import run_isolated
    ast = {"op": "field", "name": "close"}
    with pytest.raises(ValueError, match="pinned"):
        run_isolated(ast, {"close": [1]}, image="python:latest")
    from types import SimpleNamespace
    with patch("application.research_lab.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout='{"values":[1]}')) as command:
        assert run_isolated(ast, {"close": [1]}, image="research@sha256:" + "a" * 64)["values"] == [1]
        args = command.call_args.args[0]
        assert "--network=none" in args and "--read-only" in args and "--pull=never" in args
        assert not any(value in args for value in ["--mount", "-v", "--env-file", "-e"])


def test_real_account_books_are_one_snapshot_and_decimal_summed():
    from application.account_preview import account_books
    rows = [{"snap_date": "2026-09-01", "total_daily_pnl": .1, "total_daily_pct": .1, "total_mv": 100},
            {"snap_date": "2026-09-02", "total_daily_pnl": .2, "total_daily_pct": .2, "total_mv": 100.2}]
    with patch("portfolio.daily_pnl.get_recent", return_value=rows) as read:
        value = account_books()
        assert value["summary"]["period_pnl"] == .3
        assert value["scope"] == "securities_only"
        read.assert_called_once()


def test_research_profile_cannot_call_private_plan_or_books():
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from webui.api_server import app
    from webui import access_control
    identity = SimpleNamespace(agent_id="researcher", owner_app="foliant", audience="foliant",
                                scopes=frozenset({"stock.research"}), capabilities=frozenset({"foliant.selection.read"}))
    with patch.object(access_control, "_agent_authenticator", SimpleNamespace(authenticate=lambda _: identity)), \
         patch("application.account_preview.preview_account", side_effect=AssertionError("private data must not load")), \
         patch("application.account_preview.account_books", side_effect=AssertionError("private data must not load")):
        with TestClient(app) as client:
            for route in ("action-plan", "books"):
                response = client.get("/api/machine/v1/agent/portfolio/" + route, headers={"Authorization": "Bearer research"})
                assert response.status_code == 403


def test_strict_llm_json_rejects_duplicate_and_non_finite_fields():
    from analysis.strategy_policy_controller import _extract_json
    assert _extract_json('{"changes":[],"changes":[1]}') is None
    assert _extract_json('{"value":NaN}') is None


def test_securities_book_includes_dividends_without_external_deposits():
    result = reconcile_book(opening_cash=100, closing_cash=630, opening_market_value=1000,
                            closing_market_value=1000, scope="securities_only",
                            events=[{"event_id": "cash", "kind": "deposit", "cash_delta": 500},
                                    {"event_id": "div", "kind": "dividend", "cash_delta": 30}])
    assert result["pnl"] == "30.00" and result["status"] == "reconciled"


def test_star_sell_minimum_and_odd_lot_disposal():
    from analysis.decision_evaluation import equity_rules
    order = {**_order(), "side": "sell", "quantity": 100}
    assert simulate_fill(order, _bar(), equity_rules("688001"), sellable=500)["reason"] == "sell_lot_constraint"
    assert simulate_fill(order, _bar(), equity_rules("688001"), sellable=100)["quantity"] == 100


def test_missing_nav_mark_is_not_a_full_drawdown_series():
    from analysis.model_ledger import empty_ledger, mark_ledger
    ledger = empty_ledger()
    ledger["positions"] = {"600001": {"quantity": 100, "last_buy_date": None}}
    ledger = mark_ledger(ledger, trade_date="2026-09-01", prices={})
    ledger = mark_ledger(ledger, trade_date="2026-09-02", prices={"600001": 10})
    assert ledger["marks"][-1]["nav_max_drawdown_pct"] is None


def test_future_holdout_does_not_erase_current_research(store, capsule, monkeypatch):
    from jobs.decision_loop_jobs import weekly_research_cycle
    DecisionLoopService(store).seal_holdout(start_date="2026-10-01", end_date="2026-10-31")
    panel = pd.DataFrame({"symbol": ["600001"] * 20, "trade_date": pd.date_range("2026-08-01", periods=20).strftime("%Y-%m-%d"),
                          "close": list(range(10, 30)), "volume": [100] * 20, "amount": [1000] * 20})
    monkeypatch.delenv("RESEARCH_WORKER_IMAGE", raising=False)
    with patch.object(DecisionLoopService, "capsule", return_value=capsule), \
         patch.object(store, "load_daily_panel_from_manifest", return_value=panel), \
         patch.object(store, "load_financial_facts_from_manifest", return_value={}), \
         patch.object(store, "load_fund_flow_panel", return_value=pd.DataFrame()):
        result = weekly_research_cycle(store)
    trial = next(r for r in result["trials"] if r["hypothesis_id"] == "trend_exhaustion")
    assert trial["state"] == "evaluated"
    assert trial["diagnostics"]["factor_values"]["600001"]["rows"] == 20
    assert not trial["evidence"]["promotion_ready"]
    with pytest.raises(ValueError, match="not_matured"):
        conn = store.connect()
        batch_id = conn.execute("SELECT batch_id FROM research_holdout_batches").fetchone()[0]
        conn.close()
        DecisionLoopService(store).consume_holdout(batch_id, trial["trial_id"])


def test_net_evidence_requires_complete_policy_bound_comparable_marks(store):
    from application.model_evidence import model_strategy_evidence
    days = pd.bdate_range("2025-01-01", periods=160).strftime("%Y-%m-%d").tolist()
    conn = store.connect()
    conn.executemany("INSERT INTO research_trade_calendar VALUES (?,?,?)", [(d, "fixture", d) for d in days])
    for name, growth in [("pit_only", 1), ("strategy:local_value_v2", 1.001)]:
        ledger = {"marks": [{"trade_date": day, "net_asset_value": str(100000 * growth ** i),
                              "status": "verified", "policy_hash": "p1"} for i, day in enumerate(days)]}
        conn.execute("INSERT INTO research_model_portfolios VALUES (?,?,?)", (name, json.dumps(ledger), days[-1]))
    conn.commit()
    conn.close()
    evidence = model_strategy_evidence(store, "p1")["strategies"][0]
    assert evidence["effective_samples"] >= 20 and evidence["promotion_ready"]
    assert evidence["rolling_folds"]
    assert model_strategy_evidence(store, "p2")["strategies"][0]["effective_samples"] == 0
    conn = store.connect()
    ledger["marks"] = [{**r, "status": "indicative"} for r in ledger["marks"]]
    conn.execute("UPDATE research_model_portfolios SET payload=? WHERE baseline=?", (json.dumps(ledger), "strategy:local_value_v2"))
    conn.commit()
    conn.close()
    assert model_strategy_evidence(store, "p1")["strategies"][0]["effective_samples"] == 0


def test_offline_exports_paired_and_never_auto_promoted(store):
    from analysis.offline_comparison import compare_exports
    row = {"label_start": "2026-01-01", "label_end": "2026-01-06", "symbol": "600001",
           "status": "matured", "data_class": "strict_observed_pit", "net_excess_return_pct": 1}
    payload = {"dataset_id": "fixture", "execution_model": "declared-net-v1", "exports": [
        {"adapter": adapter, "code_revision": "v1", "dataset_id": "fixture", "execution_model": "declared-net-v1",
         "observations": [row]} for adapter in ("factor_ast", "qlib_offline")]}
    result = DecisionLoopService(store).compare_offline(payload)
    assert len(result["models"]) == 2 and not result["promotion_ready"]
    assert DecisionLoopService(store).compare_offline(payload) == result
    with pytest.raises(ValueError, match="sealed_holdout"):
        compare_exports(payload, sealed_intervals=[("2026-01-04", "2026-01-08")])


def test_industry_reduction_and_stale_preview_are_explicit(capsule):
    from dataclasses import asdict
    from analysis.account_action_plan import AccountLimits
    now = "2026-09-04T10:00:00+08:00"
    holdings = [{"symbol": symbol, "quantity": 1000, "sellable": 1000, "industry": "bank",
                 "execution_rules": asdict(ExecutionRules())} for symbol in ("600001", "600002")]
    quotes = {h["symbol"]: {"price": 10, "observed_at": now, "liquidity_budget": 10000} for h in holdings}
    plan = build_action_plan(capsule, holdings, quotes, holdings_version="v1", owner_id="owner", now=now,
                             cash=10000, limits=AccountLimits(max_position=.5, max_turnover=.3))
    assert next(a for a in plan["alternatives"] if a["kind"] == "reduce")["actions"]
