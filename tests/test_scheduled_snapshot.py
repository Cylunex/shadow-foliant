from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from application.scheduled_snapshot import ScheduledSnapshotService


NOW = datetime(2026, 9, 10, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
ROOT = Path(__file__).resolve().parents[1]


class CalendarStore:
    def __init__(self, *, ready=True, coverage="2026-09-10", latest="2026-09-10"):
        self.value = {
            "ready": ready,
            "covered_provider_count": 2 if ready else 1,
            "coverage_through_date": coverage,
            "latest_confirmed_open_date": latest,
        }

    def calendar_consensus(self, _day, *, inclusive=False):
        assert inclusive is True
        return dict(self.value)


def selection(*, day="2026-09-10", market_as_of: str | None = None,
              with_wencai=True, independent_day: str | None = None,
              with_trade_plans: bool = True,
              independent_symbols: list[str] | None = None):
    market_as_of = market_as_of or day
    top15 = [{
        "symbol": f"600{i:03d}", "name": f"候选{i}", "rank": i,
        **({"trade_plan": {"available": True, "action": "hold", "reason": "规则计划"}}
           if with_trade_plans else {}),
    } for i in range(1, 16)]
    independent_top15 = top15 if independent_symbols is None else [
        {"symbol": symbol, "name": f"独立{i}", "rank": i, "total_score": 100 - i}
        for i, symbol in enumerate(independent_symbols, 1)
    ]
    strategies = {
        name: {"strategy_id": f"wencai-{index}", "strategy_version": "v1",
               "status": "ready", "picks": [{"symbol": f"00000{index}", "name": name}]}
        for index, name in enumerate(("主力资金", "低价擒牛", "小市值", "净利增长", "低估值"), 1)
    } if with_wencai else {}
    return {
        "status": "complete",
        "warnings": [],
        "provenance": {"run_id": "formal-run", "market_as_of": market_as_of,
                       "input_manifest_id": "manifest", "policy_hash": "policy",
                       "code_revision": "revision"},
        "data": {
            "selection_date": day,
            "formal_top15": top15,
            "formal_top5": top15[:5],
            "references": {
                "wencai": {"executed_at": NOW.isoformat(), "strategies": strategies},
                "independent": {
                    "status": "ready", "strategy_id": "codex-independent",
                    "strategy_version": "codex-independent-v1", "strategy_hash": "fixed",
                    "manifest_id": "manifest", "input_snapshot_id": "independent-snapshot",
                    "market_as_of": independent_day if independent_day is not None else market_as_of,
                    "weights": {"fundamental_quality": 30, "medium_trend": 25,
                                "valuation": 20, "flow_liquidity": 15,
                                "risk_discount": 10},
                    "top15": independent_top15, "top5": independent_top15[:5],
                    "independence_boundary": "immutable_manifest_inputs_only",
                },
            },
            "selection_comparison": {
                "availability": {"formal": True, "independent": True,
                                 "wencai": bool(with_wencai)},
                "pairwise": {}, "triple": None,
            },
        },
    }


def capsule():
    rows = [{"symbol": f"600{i:03d}", "industry": "示例", "themes": ["测试"]}
            for i in range(1, 16)]
    return {
        "capsule_id": "dc_example", "run_id": "formal-run",
        "opportunity_set": {"top15": rows, "top5": rows[:5]},
    }


def cockpit(**_kwargs):
    return {"status": "success", "meta": {"as_of": NOW.isoformat()}, "data": {
        "tasks": {"total": 10, "failed_recent": [], "disabled_core": [],
                  "running_manual": []},
        "holding_count": 2, "active_recommendation_count": 0,
        "active_signal_count": 0, "datahub": {}, "portfolio_policy": {},
        "strategy_deployment": {},
    }}


def context():
    return {"watermark": "same-watermark", "holdings": [
        {"code": "600001", "name": "持仓甲", "cost_price": 9, "quantity": 100},
        {"code": "000001", "name": "持仓乙", "cost_price": 10, "quantity": 200,
         "note": "must-not-be-projected"},
    ]}


def build_service(
    *, store=None, selection_value=None, quote_spy=None, context_reader=context,
    clock=lambda: NOW, job_runs_reader=lambda **_kwargs: [],
    quote_time=None, intraday_value=None,
    intraday_projector=None,
    outcome_stats_reader=lambda **_kwargs: {"dimension": "source_type", "days": 180,
                                            "buckets": []},
    cash_reader=lambda: {"status": "missing", "amount": None,
                         "reason": "confirmed_cash_balance_missing"},
    security_metadata_reader=lambda _day: {
        "stock_symbols": {"000001", *(f"600{i:03d}" for i in range(1, 16))},
        "fund_symbols": set(),
        "sources": [{"source": "test_security_master"}],
        "errors": [],
    },
    strategy_evidence_reader=lambda **_kwargs: {
        "horizon_days": 5, "lookback_days": 180, "strategies": [],
        "portfolio_comparison": {"matured_runs": 0, "avg_satellite_marginal_pct": None},
        "evidence_snapshot_id": "empty-evidence",
    },
    external_research_reader=lambda: {"status": "missing"},
    missing_quote_symbols=(),
):
    def quotes(symbols):
        if quote_spy is not None:
            quote_spy.append(list(symbols))
        return {symbol: {"name": symbol, "price": 10, "change_pct": 1,
                         "quote_time": (quote_time or NOW).isoformat(), "volume": 1000,
                         "amount_wan": 100, "limit_up": 11, "limit_down": 9}
                for symbol in symbols if symbol not in set(missing_quote_symbols)}

    if intraday_projector is None:
        def intraday_projector(**kwargs):
            previous = kwargs.get("previous") or {}
            current = kwargs["now"]
            return {
                **previous,
                "plans": (
                    previous.get("plans") or {}
                    if previous.get("selection_run_id") == "formal-run" else {}
                ),
                "status": "success",
                "trade_date": current.date().isoformat(),
                "generated_at": current.isoformat(timespec="seconds"),
                "selection_run_id": previous.get("selection_run_id") or "formal-run",
            }

    return ScheduledSnapshotService(
        store=store or CalendarStore(),
        selection_reader=lambda: selection_value or selection(),
        cockpit_reader=cockpit,
        context_reader=context_reader,
        capsule_reader=capsule,
        intraday_reader=lambda: intraday_value or {},
        intraday_projector=intraday_projector,
        quote_loader=quotes,
        job_runs_reader=job_runs_reader,
        outcome_stats_reader=outcome_stats_reader,
        strategy_evidence_reader=strategy_evidence_reader,
        external_research_reader=external_research_reader,
        cash_reader=cash_reader,
        security_metadata_reader=security_metadata_reader,
        clock=clock,
    )


def test_snapshot_batches_top15_and_holdings_once_and_keeps_as_of():
    calls = []
    result = build_service(quote_spy=calls).read(owner_id="scheduled-agent")
    snapshot = result["data"]
    assert len(calls) == 1
    assert calls[0] == sorted({*(f"600{i:03d}" for i in range(1, 16)), "000001"})
    assert snapshot["quotes"]["batch_count"] == 1
    assert snapshot["quotes"]["requested_count"] == 16
    assert all(row["as_of"] == NOW.isoformat() for row in snapshot["quotes"]["rows"])
    assert snapshot["trade_plans"]["auto_execution"] is False
    assert len(snapshot["trade_plans"]["formal"]) == 15
    assert len(snapshot["trade_plans"]["formal_candidate_follow_up"]) == 15
    assert snapshot["trade_plans"]["next_premarket_check"]["auto_execution"] is False
    assert snapshot["trade_plans"]["cash_policy"]["new_or_add_positions_allowed"] is False
    assert snapshot["post_close_review"]["status"] == "pending"
    assert snapshot["strategy_adjustment_proposals"]["guardrails"]["auto_apply"] is False
    assert "note" not in snapshot["holdings"]["rows"][1]
    assert snapshot["independent_selection"]["status"] == "complete"
    assert len(snapshot["independent_selection"]["top5"]) == 5
    assert snapshot["quality"]["sections"]["independent_selection"] == "complete"
    assert [row["name"] for row in snapshot["wencai_reference"]["strategies"]] == [
        "低价擒牛", "低估值", "主力资金", "小市值", "净利增长",
    ]


def test_external_research_is_optional_current_overlay_and_never_a_price_authority():
    rows = [{
        "symbol": f"600{i:03d}", "name": f"独立{i}", "rank": i,
        "base_rank": i, "base_score": 100 - i,
        "event_adjustment": 8 if i == 5 else 0,
        "risk_veto": False, "final_score": 108 - i if i == 5 else 100 - i,
        "evidence_ids": ["ere-1"] if i == 5 else [],
    } for i in range(1, 16)]
    rows.sort(key=lambda row: (-row["final_score"], row["symbol"]))
    external = {
        "status": "ready", "channel": "codex-external-independent-v1",
        "overlay": {
            "selection_run_id": "formal-run",
            "base_strategy_version": "codex-independent-v1",
            "base_input_snapshot_id": "independent-snapshot",
            "decision_as_of": NOW.isoformat(), "ranking_locked_at": NOW.isoformat(),
            "market_regime": "sideways", "top15": rows,
            "identity_boundary": "independent-only", "price_authority": "none",
        },
        "evidence": [{"evidence_id": "ere-1", "source_type": "announcement"}],
        "news_watchlist": [{"symbol": "300001", "status": "observation_only"}],
        "tuning_proposals": [], "outcomes": {"buckets": []},
    }

    result = build_service(external_research_reader=lambda: external).read(
        owner_id="scheduled-agent"
    )["data"]
    projected = result["external_independent_research"]
    assert projected["status"] == "complete"
    assert len(projected["top15"]) == 15
    assert projected["top5"][0]["symbol"] == "600005"
    assert projected["formal_membership_unchanged"] is True
    assert projected["external_can_create_execution_price"] is False
    assert projected["auto_apply"] is False
    assert projected["auto_execution"] is False
    assert result["source_comparison"]["availability"]["external_independent"] is True
    assert result["quality"]["status"] == "complete"


def test_quote_batch_unions_holdings_formal_independent_and_external_with_source_coverage():
    calls = []
    independent_symbols = [f"601{i:03d}" for i in range(1, 16)]
    rows = [{
        "symbol": symbol, "name": f"独立{i}", "rank": i,
        "base_rank": i, "base_score": 100 - i,
        "event_adjustment": 0, "risk_veto": False,
        "final_score": 100 - i, "evidence_ids": [],
    } for i, symbol in enumerate(independent_symbols, 1)]
    external = {
        "status": "ready", "channel": "codex-external-independent-v1",
        "overlay": {
            "selection_run_id": "formal-run",
            "base_strategy_version": "codex-independent-v1",
            "base_input_snapshot_id": "independent-snapshot",
            "decision_as_of": NOW.isoformat(), "ranking_locked_at": NOW.isoformat(),
            "market_regime": "sideways", "top15": rows,
        },
    }
    result = build_service(
        selection_value=selection(independent_symbols=independent_symbols),
        external_research_reader=lambda: external,
        quote_spy=calls,
        missing_quote_symbols={"601015"},
    ).read(owner_id="scheduled-agent")["data"]

    expected = {
        *(f"600{i:03d}" for i in range(1, 16)),
        *independent_symbols, "000001",
    }
    assert len(calls) == 1
    assert set(calls[0]) == expected
    assert result["quotes"]["requested_count"] == 31
    assert {row["symbol"] for row in result["quotes"]["rows"]} == expected
    coverage = result["quotes"]["source_coverage"]
    assert coverage["holdings"]["requested_count"] == 2
    assert coverage["formal_top15"]["coverage"] == 1.0
    assert coverage["independent_top15"] == {
        "status": "degraded", "requested_count": 15, "available_count": 14,
        "coverage": round(14 / 15, 6), "missing_symbols": ["601015"],
    }
    assert coverage["external_overlay_top15"]["missing_symbols"] == ["601015"]
    projected = result["external_independent_research"]
    assert projected["status"] == "complete"
    assert len(projected["top15"]) == 15
    assert projected["pricing_guard"] == {
        "status": "blocked", "ranking_preserved": True,
        "execution_price_available": False, "missing_symbols": ["601015"],
        "blockers": ["independent_or_external_quote_coverage_incomplete"],
    }
    assert projected["external_can_create_execution_price"] is False


def test_external_research_with_wrong_base_snapshot_is_stale_but_non_blocking():
    rows = [{"symbol": f"600{i:03d}", "name": f"过期{i}", "rank": i}
            for i in range(1, 16)]
    external = {
        "status": "ready", "channel": "codex-external-independent-v1",
        "overlay": {
            "selection_run_id": "formal-run",
            "base_strategy_version": "codex-independent-v1",
            "base_input_snapshot_id": "different-snapshot",
            "decision_as_of": NOW.isoformat(), "top15": rows,
        },
    }
    result = build_service(external_research_reader=lambda: external).read(
        owner_id="scheduled-agent"
    )["data"]
    assert result["external_independent_research"]["status"] == "stale"
    assert result["external_independent_research"]["historical_top15_count"] == 15
    assert result["external_independent_research"]["top15"] == []
    assert result["external_independent_research"]["top5"] == []
    assert result["external_independent_research"]["comparison"] is None
    assert result["external_independent_research"]["pricing_guard"]["ranking_preserved"] is False
    assert result["source_comparison"]["external_top5"] is None
    assert "external_independent" in result["source_comparison"]["unavailable_sources"]
    assert result["quality"]["status"] == "complete"
    assert "external_independent_research" in result["quality"]["optional_degradations"]
    from scripts import foliant_scheduled_snapshot as cli
    assert "旧排名不参与判断" in cli.render_qq_report(result)[1]


def test_intraday_actions_are_recomputed_after_quotes_and_bound_to_same_batch():
    calls = []
    pricing_at = NOW + timedelta(seconds=5)
    clock_values = iter((NOW, pricing_at))
    old = {
        "selection_run_id": "formal-run",
        "trade_date": NOW.date().isoformat(),
        "generated_at": (NOW - timedelta(minutes=20)).isoformat(),
        "holdings": [{"symbol": "000001", "action": "sell"}],
        "plans": {"000001": {"available": True, "action": "hold"}},
    }

    def projector(**kwargs):
        assert calls, "the single quote batch must be loaded before action projection"
        assert set(kwargs["raw_quotes"]) == set(calls[0])
        return {
            **kwargs["previous"],
            "status": "success",
            "trade_date": kwargs["now"].date().isoformat(),
            "generated_at": kwargs["now"].isoformat(timespec="seconds"),
            "selection_run_id": "formal-run",
            "holdings": [{"symbol": "000001", "action": "hold"}],
            "portfolio_action_guard": {"status": "applied", "guarded_count": 1},
        }

    snapshot = build_service(
        quote_spy=calls,
        quote_time=pricing_at,
        clock=lambda: next(clock_values),
        intraday_value={"data": old},
        intraday_projector=projector,
    ).read(owner_id="scheduled-agent")["data"]

    authority = snapshot["trade_plans"]["holding_actions_authority"]
    assert len(calls) == 1
    assert snapshot["trade_plans"]["intraday_as_of"] == pricing_at.isoformat(timespec="seconds")
    assert snapshot["quotes"]["captured_at"] == pricing_at.isoformat(timespec="seconds")
    assert snapshot["trade_plans"]["holding_actions"][0]["action"] == "hold"
    assert authority["status"] == "current"
    assert authority["quote_binding"] == "same_snapshot_quote_batch"
    assert authority["pricing_snapshot_id"] == snapshot["quotes"]["snapshot_id"]
    assert snapshot["trade_plans"]["intraday_plan_binding"]["quote_binding"] == (
        "same_snapshot_quote_batch"
    )
    assert snapshot["trade_plans"]["portfolio_action_guard"]["guarded_count"] == 1


def test_intraday_projection_failure_is_optional_when_fixed_budget_risk_is_complete():
    old = {
        "selection_run_id": "formal-run",
        "trade_date": NOW.date().isoformat(),
        "generated_at": (NOW - timedelta(minutes=20)).isoformat(),
        "holdings": [{"symbol": "000001", "action": "sell"}],
    }
    snapshot = build_service(
        intraday_value={"data": old},
        intraday_projector=lambda **_kwargs: {},
    ).read(owner_id="scheduled-agent")["data"]

    authority = snapshot["trade_plans"]["holding_actions_authority"]
    assert snapshot["trade_plans"]["status"] == "complete"
    assert snapshot["trade_plans"]["status_basis"] == (
        "fixed_stock_budget_risk_and_pricing_complete"
    )
    assert snapshot["trade_plans"]["optional_degradations"] == [{
        "code": "holding_actions_not_bound_to_current_quotes",
        "affects_snapshot_quality": False,
    }]
    assert snapshot["quality"]["blocking_sections"] == []
    assert authority["status"] == "stale_or_missing"
    assert authority["quote_binding"] == "persisted_reference"


def test_unknown_calendar_never_uses_weekday_fallback():
    result = build_service(store=CalendarStore(ready=False, coverage="2026-09-09")).read(
        owner_id="scheduled-agent"
    )["data"]
    assert NOW.weekday() < 5
    assert result["trading_day"]["confirmed"] is False
    assert result["trading_day"]["is_trading_day"] is None
    assert result["trading_day"]["basis"] == "two_source_consensus_required"


def test_confirmed_closed_day_and_stale_formal_are_explicit():
    store = CalendarStore(latest="2026-09-09")
    result = build_service(store=store, selection_value=selection(day="2026-09-08")).read(
        owner_id="scheduled-agent"
    )["data"]
    assert result["trading_day"]["confirmed"] is True
    assert result["trading_day"]["is_trading_day"] is False
    assert result["formal_selection"]["status"] == "stale"
    assert result["status"] == "degraded"


def test_independent_selection_stale_when_as_of_mismatch():
    value = selection(day="2026-09-11", independent_day="2026-09-10")
    result = build_service(selection_value=value).read(owner_id="scheduled-agent")["data"]
    assert result["formal_selection"]["selection_date"] == "2026-09-11"
    assert result["independent_selection"]["status"] == "stale"
    assert result["independent_selection"]["expected_market_as_of"] == "2026-09-11"
    assert result["independent_selection"]["reason"] == "independent result date mismatch"
    assert any("independent_selection.market_as_of(2026-09-10)" in item
               for item in result["independent_selection"]["warnings"])
    assert result["quality"]["sections"]["independent_selection"] == "stale"
    assert result["status"] == "degraded"


def test_monday_independent_matches_formal_market_cutoff_not_selection_date():
    value = selection(
        day="2026-09-14", market_as_of="2026-09-11",
        independent_day="2026-09-11",
    )
    result = build_service(selection_value=value).read(owner_id="scheduled-agent")["data"]
    assert result["formal_selection"]["selection_date"] == "2026-09-14"
    assert result["formal_selection"]["market_as_of"] == "2026-09-11"
    assert result["independent_selection"]["status"] == "complete"
    assert result["independent_selection"]["expected_market_as_of"] == "2026-09-11"
    assert result["independent_selection"]["warnings"] == []


def test_risk_preview_uses_same_quote_freshness_window_and_snapshot():
    result = build_service(quote_time=NOW - timedelta(minutes=4)).read(
        owner_id="scheduled-agent"
    )["data"]
    risk = result["trade_plans"]["portfolio_risk"]
    assert result["quotes"]["status"] == "success"
    assert result["quotes"]["quote_ttl_seconds"] == 480
    assert risk["risk_snapshot"]["status"] == "complete"
    assert risk["risk_snapshot"]["missing_prices"] == []
    assert risk["blockers"] == []
    assert risk["cash_basis"] == "user_declared_stock_budget"
    assert risk["pricing_snapshot"]["snapshot_id"] == result["quotes"]["snapshot_id"]
    assert risk["pricing_snapshot"]["oldest_as_of"] == result["quotes"]["oldest_usable_as_of"]


def test_formal_follow_up_binds_current_intraday_trade_plans():
    plans = {
        f"600{i:03d}": {
            "available": True, "action": "hold", "entry_low": 9.8,
            "entry_high": 10.0, "stop_loss": 9.2, "target_price": 11.6,
            "plan_as_of": "2026-09-10", "price_basis": "formal_manifest_qfq",
        }
        for i in range(1, 16)
    }
    intraday = {"data": {
        "selection_run_id": "formal-run", "generated_at": NOW.isoformat(),
        "plans": plans,
    }}
    result = build_service(
        selection_value=selection(with_trade_plans=False), intraday_value=intraday,
    ).read(owner_id="scheduled-agent")["data"]
    follow_up = result["trade_plans"]["formal_candidate_follow_up"]
    assert len(result["trade_plans"]["formal"]) == 15
    assert len(follow_up) == 15
    assert all(row["status"] == "ready" for row in follow_up)
    assert all(row["trade_plan_source"] == "current_intraday_snapshot" for row in follow_up)
    assert follow_up[0]["trade_plan"] == {
        "available": True, "action": "hold", "entry_low": 9.8,
        "entry_high": 10.0, "stop_loss": 9.2, "target_price": 11.6,
        "plan_as_of": "2026-09-10", "price_basis": "formal_manifest_qfq",
    }
    assert result["trade_plans"]["intraday_plan_binding"]["status"] == "current"


def test_formal_follow_up_rejects_plans_from_another_selection_run():
    intraday = {"data": {
        "selection_run_id": "old-formal-run", "generated_at": NOW.isoformat(),
        "plans": {"600001": {"available": True, "entry_low": 9.8}},
    }}
    result = build_service(
        selection_value=selection(with_trade_plans=False), intraday_value=intraday,
    ).read(owner_id="scheduled-agent")["data"]
    follow_up = result["trade_plans"]["formal_candidate_follow_up"]
    assert result["trade_plans"]["formal"] == []
    assert all(row["status"] == "blocked" for row in follow_up)
    assert all("trade_plan_missing" in row["blockers"] for row in follow_up)
    assert result["trade_plans"]["intraday_plan_binding"]["status"] == "stale_or_missing"


def test_user_declared_stock_budget_supersedes_legacy_cash_fact_without_enabling_additions():
    result = build_service(cash_reader=lambda: {
        "status": "confirmed", "amount": "1000.00", "as_of": "2026-09-10",
        "basis": "confirmed_account_fact",
    }).read(owner_id="scheduled-agent")["data"]
    risk = result["trade_plans"]["portfolio_risk"]
    cash_policy = result["trade_plans"]["cash_policy"]
    assert "cash_unknown" not in (risk.get("blockers") or [])
    assert risk["risk_snapshot"]["cash_known"] is True
    assert risk["risk_snapshot"]["denominator_scope"] == "full_account"
    assert cash_policy["cash_basis"] == "user_declared_stock_budget"
    assert cash_policy["cash_status"] == "complete"
    assert cash_policy["stock_budget"]["available_cash_cny"] == 297000
    assert cash_policy["broker_cash_balance"] is False
    assert cash_policy["new_or_add_positions_allowed"] is False


def test_missing_legacy_cash_is_non_blocking_when_fixed_budget_is_complete():
    result = build_service().read(owner_id="scheduled-agent")["data"]
    plans = result["trade_plans"]
    cash_policy = plans["cash_policy"]
    cash_contract = plans["source_contracts"]["cash_balance"]

    assert cash_policy["stock_budget"]["status"] == "complete"
    assert cash_policy["legacy_cash_fact"] == {
        "status": "missing",
        "role": "non_blocking_metadata",
        "affects_snapshot_quality": False,
    }
    assert cash_contract["legacy_confirmed_cash_fact_status"] == "missing"
    assert cash_contract["legacy_confirmed_cash_fact_role"] == "non_blocking_metadata"
    assert cash_contract["legacy_confirmed_cash_fact_affects_snapshot_quality"] is False
    assert plans["status"] == "complete"
    assert result["quality"]["blocking_sections"] == []


def test_missing_wencai_does_not_change_formal_candidates():
    value = selection(with_wencai=False)
    expected = [row["symbol"] for row in value["data"]["formal_top15"]]
    result = build_service(selection_value=value).read(owner_id="scheduled-agent")["data"]
    assert [row["symbol"] for row in result["formal_selection"]["formal_top15"]] == expected
    assert result["wencai_reference"]["status"] == "missing"
    assert result["wencai_reference"]["reference_affects_membership"] is False
    replacement = result["wencai_reference"]["official_replacement_contract"]
    assert replacement["provider"] == "fuyao_aicubes"
    assert replacement["identity"] == "distinct_official_provider_not_wencai"
    assert replacement["must_not_be_labeled_as_wencai"] is True


def test_holdings_watermark_change_fails_risk_plan_closed():
    reads = [context(), {**context(), "watermark": "changed"}]
    result = build_service(context_reader=lambda: reads.pop(0)).read(
        owner_id="scheduled-agent"
    )["data"]
    assert result["holdings"]["status"] == "complete"
    assert result["trade_plans"]["status"] == "stale"
    assert result["trade_plans"]["portfolio_risk"]["error_code"] == "holdings_changed_during_preview"


def test_post_close_review_uses_posterior_threshold_and_never_auto_applies():
    evening = NOW.replace(hour=21, minute=0)
    jobs = [
        {"job_name": "eod_outcomes", "started_at": evening.isoformat(),
         "finished_at": evening.isoformat(), "status": "success",
         "error": "rec: checked=8 tp=2 sl=1 | signals: eval=40 hit=11 miss=29 | decision_loop=complete"},
        {"job_name": "daily_backtest", "started_at": evening.isoformat(),
         "finished_at": evening.isoformat(), "status": "success",
         "error": "https://secret.invalid must-not-leak"},
        {"job_name": "portfolio_indicator_snapshot", "started_at": evening.isoformat(),
         "finished_at": evening.isoformat(), "status": "success", "error": ""},
        {"job_name": "research_data_sync_retry", "started_at": evening.isoformat(),
         "finished_at": evening.isoformat(), "status": "skipped",
         "error": "daily market already complete"},
    ]
    outcomes = {
        "dimension": "source_type", "days": 180,
        "buckets": [
            {"bucket": "exit_advice", "n": 40, "hit": 11, "miss": 29,
             "neutral": 0, "directional_n": 40, "minimum_feedback_samples": 30,
             "sample_status": "evaluated", "posterior_hit_rate_pct": 29.5,
             "performance_factor": 0.8},
            {"bucket": "small_sample", "n": 12, "hit": 3, "miss": 9,
             "neutral": 0, "directional_n": 12, "minimum_feedback_samples": 30,
             "sample_status": "observational", "performance_factor": 0.7},
        ],
    }
    evidence = {
        "horizon_days": 5, "lookback_days": 180,
        "evidence_snapshot_id": "strategy-evidence",
        "strategies": [{
            "strategy_id": "pit-core", "lane": "core", "sample_size": 80,
            "effective_samples": 0,
            "promotion_blocker": "price_labels_are_not_independent_executable_net_evidence",
        }],
        "portfolio_comparison": {"matured_runs": 12,
                                 "avg_satellite_marginal_pct": -0.6},
    }
    outcome_calls = []

    def read_outcomes(**kwargs):
        outcome_calls.append(kwargs)
        return outcomes

    snapshot = build_service(
        clock=lambda: evening,
        job_runs_reader=lambda **_kwargs: jobs,
        outcome_stats_reader=read_outcomes,
        strategy_evidence_reader=lambda **_kwargs: evidence,
    ).read(owner_id="scheduled-agent")["data"]

    assert outcome_calls == [{"dimension": "source_type", "days": 180,
                              "ensure_tables": False}]
    assert snapshot["post_close_review"]["status"] == "complete"
    outcomes_job = next(row for row in snapshot["post_close_review"]["jobs"]
                        if row["job_name"] == "eod_outcomes")
    assert outcomes_job["metrics"]["signals_evaluated"] == 40
    assert "secret.invalid" not in str(snapshot)
    proposals = snapshot["strategy_adjustment_proposals"]
    assert proposals["proposal_count"] == 1
    assert proposals["proposals"][0]["direction"] == "downweight"
    assert proposals["proposals"][0]["proposed_multiplier"] == 0.95
    assert proposals["blocked_strategy_count"] == 1
    assert proposals["guardrails"]["auto_apply"] is False


def test_post_close_snapshot_uses_closing_marks_and_exposes_next_session_outputs():
    evening = NOW.replace(hour=20, minute=46)
    close_time = NOW.replace(hour=16, minute=15)
    jobs = [
        {"job_name": name, "started_at": evening.isoformat(),
         "finished_at": evening.isoformat(), "status": "success", "error": ""}
        for name in (
            "portfolio_indicator_snapshot", "eod_outcomes", "daily_backtest",
            "research_data_sync_retry",
        )
    ]
    holding_plan = {
        "available": True, "action": "hold", "action_cn": "不动",
        "entry_low": 9.8, "entry_high": 10.1,
        "stop_loss": 9.2, "target_price": 11.5,
        "price_basis": "persisted-rule-plan",
    }
    snapshot = build_service(
        clock=lambda: evening,
        quote_time=close_time,
            intraday_value={"data": {
                "selection_run_id": "formal-run", "plans": {"000001": holding_plan},
            }},
        job_runs_reader=lambda **_kwargs: jobs,
    ).read(owner_id="scheduled-agent")["data"]

    assert snapshot["quotes"]["status"] == "success"
    assert all(row["freshness"] == "closing_current"
               for row in snapshot["quotes"]["rows"])
    assert snapshot["trade_plans"]["pricing_status"] == "success"
    assert snapshot["trade_plans"]["pricing_context"] == "post_close"
    risk = snapshot["trade_plans"]["portfolio_risk"]
    assert risk["risk_snapshot"]["status"] == "complete"
    assert risk["risk_snapshot"]["missing_prices"] == []
    assert float(risk["risk_snapshot"]["securities_value"]) > 0
    assert risk["stress_scenarios"]
    assert snapshot["holdings_review"]["status"] == "complete"
    assert snapshot["holdings_review"]["count"] == 2
    assert all(row["reference_close"] == 10
               for row in snapshot["holdings_review"]["rows"])
    assert snapshot["next_session_plan"]["status"] == "complete"
    assert snapshot["next_session_plan"]["count"] == 16
    assert snapshot["next_session_plan"]["auto_execution"] is False
    assert snapshot["source_comparison"]["status"] == "complete"
    assert snapshot["source_comparison"]["availability"] == {
        "formal": True, "independent": True, "wencai": True,
        "external_independent": False,
    }
    assert snapshot["post_close_review"]["holdings_review_status"] == "complete"
    assert snapshot["post_close_review"]["next_session_plan_status"] == "complete"
    assert snapshot["trade_plans"]["intraday_plan_binding"]["status"] == "historical_reference"
    assert snapshot["trade_plans"]["holding_actions_authority"]["status"] == "historical_reference"
    shared = snapshot["holdings_review"]["pricing_snapshot"]
    assert shared == snapshot["next_session_plan"]["pricing_snapshot"]
    assert shared["snapshot_id"] == risk["pricing_snapshot"]["snapshot_id"]
    assert shared["as_of"] == risk["pricing_snapshot"]["oldest_as_of"]
    assert shared["mode"] == "post_close"


def test_four_report_phases_keep_expected_authority_and_pending_semantics():
    jobs = [
        {"job_name": name, "started_at": "2026-09-10T20:30:00+08:00",
         "finished_at": "2026-09-10T20:31:00+08:00", "status": "success", "error": ""}
        for name in (
            "portfolio_indicator_snapshot", "eod_outcomes", "daily_backtest",
            "research_data_sync_retry",
        )
    ]
    holding_plan = {
        "available": True, "action": "hold", "action_cn": "不动",
        "stop_loss": 9, "target_price": 12, "price_basis": "persisted-rule-plan",
    }
    intraday = {"data": {
        "selection_run_id": "formal-run", "generated_at": "2026-09-10T14:49:00+08:00",
        "plans": {"000001": holding_plan, "600001": holding_plan},
    }}
    cases = (
        (10, 5, "intraday", "current", "intraday_rule_plan"),
        (14, 30, "intraday", "current", "intraday_rule_plan"),
        (18, 23, "post_close_pending", "historical_reference", "pending_post_close_review"),
        (20, 46, "post_close_review", "historical_reference", "next_session_plan"),
    )
    for hour, minute, phase, binding, authority in cases:
        current = NOW.replace(hour=hour, minute=minute)
        snapshot = build_service(
            clock=lambda current=current: current,
            quote_time=current,
            selection_value=selection(with_wencai=False),
            intraday_value=intraday,
            job_runs_reader=lambda **_kwargs: jobs,
            security_metadata_reader=lambda _day: {
                "stock_symbols": set(), "fund_symbols": set(),
                "sources": [], "errors": ["metadata_unavailable"],
            },
        ).read(owner_id="scheduled-agent")["data"]
        assert snapshot["phase"] == phase
        assert snapshot["trade_plans"]["intraday_plan_binding"]["status"] == binding
        assert snapshot["trade_plans"]["current_authority"] == authority
        assert snapshot["quality"]["optional_degradations"] == [
            "wencai_reference", "external_independent_research",
        ]
        assert snapshot["trade_plans"]["cash_policy"]["buy_side"]["status"] == "blocked"
        assert snapshot["trade_plans"]["cash_policy"]["sell_side"]["status"] == "available"
        assert "trade_plans" not in snapshot["quality"]["blocking_sections"]
        if phase == "post_close_pending":
            assert snapshot["status"] == "complete"
            assert snapshot["quality"]["pending_is_normal"] is True
        if phase == "post_close_review":
            assert snapshot["post_close_review"]["status"] == "complete"
            assert snapshot["holdings_review"]["status"] == "complete"
            assert snapshot["next_session_plan"]["status"] == "complete"


def test_post_close_omits_stale_intraday_holding_actions():
    evening = NOW.replace(hour=20, minute=46)
    old = {"data": {
        "generated_at": NOW.replace(hour=14, minute=30).isoformat(),
        "holdings": [
            {"symbol": "000001", "action": "hold", "reason": "旧盘中证据" * 100}
            for _ in range(100)
        ],
    }}
    snapshot = build_service(
        clock=lambda: evening, quote_time=evening, intraday_value=old,
    ).read(owner_id="scheduled-agent")["data"]
    assert snapshot["phase"] == "post_close_review"
    assert snapshot["trade_plans"]["holding_actions"] == []
    assert snapshot["trade_plans"]["holding_actions_omitted_count"] == 100
    assert snapshot["trade_plans"]["holding_actions_authority"]["status"] == "historical_reference"


def test_post_close_expired_intraday_add_gate_is_expected_not_blocking():
    evening = NOW.replace(hour=18, minute=23)

    def degraded_cockpit(**_kwargs):
        return {"status": "degraded", "meta": {"as_of": evening.isoformat()}, "data": {
            "tasks": {"total": 47, "failed_recent": [], "disabled_core": [],
                      "running_manual": []},
            "holding_count": 53, "active_recommendation_count": 0,
            "active_signal_count": 0,
            "datahub": {"sources": {"primary": {"fail": 1},
                                     "fallback": {"ok": 1}}},
            "portfolio_policy": {
                "fail_closed": True,
                "market_add_signal": {"stale": True, "fresh": False},
            },
            "strategy_deployment": {},
        }}

    service = build_service(clock=lambda: evening, quote_time=evening)
    service.cockpit_reader = degraded_cockpit
    snapshot = service.read(owner_id="scheduled-agent")["data"]

    assert snapshot["cockpit"]["raw_status"] == "degraded"
    assert snapshot["cockpit"]["phase_quality_status"] == "complete"
    assert snapshot["cockpit"]["expected_phase_degradations"] == [
        "intraday_market_add_signal_expired_after_close",
    ]
    assert snapshot["quality"]["sections"]["cockpit"] == "complete"
    assert snapshot["status"] == "complete"


def test_auxiliary_quote_failure_is_non_blocking_when_fallback_coverage_is_complete():
    def provider_only_degraded_cockpit(**_kwargs):
        return {"status": "degraded", "meta": {"as_of": NOW.isoformat()}, "data": {
            "tasks": {"total": 47, "failed_recent": [], "disabled_core": [],
                      "running_manual": []},
            "holding_count": 2, "active_recommendation_count": 0,
            "active_signal_count": 0,
            "degradation_reasons": ["optional_quote_provider_degraded"],
            "datahub": {"sources": {
                "quotes:a_stock": {"ok": 2, "fail": 0, "streak_fail": 0},
                "quotes:fuyao_aicubes": {
                    "ok": 0, "fail": 2, "streak_fail": 2,
                    "failure_code": 5001,
                    "failure_category": "upstream_unavailable",
                    "http_status": 200,
                },
            }},
            "portfolio_policy": {"fail_closed": False},
            "strategy_deployment": {},
        }}

    service = build_service()
    service.cockpit_reader = provider_only_degraded_cockpit
    snapshot = service.read(owner_id="scheduled-agent")["data"]

    assert snapshot["quotes"]["requested_count"] == snapshot["quotes"]["available_count"]
    assert snapshot["cockpit"]["raw_status"] == "degraded"
    assert snapshot["cockpit"]["phase_quality_status"] == "complete"
    assert snapshot["quality"]["sections"]["cockpit"] == "complete"
    assert snapshot["quality"]["optional_source_degradations"] == [
        "quotes:fuyao_aicubes",
    ]
    assert snapshot["cockpit"]["optional_provider_degradations"][0]["failure_code"] == 5001
    assert snapshot["status"] == "complete"


def test_missing_fresh_add_signal_remains_blocking_at_pricing_boundary():
    def required_dimension_degraded_cockpit(**_kwargs):
        return {"status": "degraded", "meta": {"as_of": NOW.isoformat()}, "data": {
            "tasks": {"total": 47, "failed_recent": [], "disabled_core": [],
                      "running_manual": []},
            "holding_count": 2, "active_recommendation_count": 0,
            "active_signal_count": 0,
            "degradation_reasons": ["fresh_market_add_signal_missing"],
            "blocking_dimensions": [{
                "dimension": "fresh_market_add_signal",
                "status": "stale_or_missing",
                "affected_decisions": ["new_positions", "add_positions"],
                "decision_boundary": "pricing_only_no_buy_authorization",
            }],
            "datahub": {"sources": {
                "quotes:a_stock": {"ok": 2, "fail": 0, "streak_fail": 0},
                "quotes:fuyao_aicubes": {"ok": 0, "fail": 2, "streak_fail": 2},
            }},
            "portfolio_policy": {
                "fail_closed": True,
                "market_add_signal": {"stale": True, "fresh": False},
            },
            "strategy_deployment": {},
        }}

    service = build_service()
    service.cockpit_reader = required_dimension_degraded_cockpit
    snapshot = service.read(owner_id="scheduled-agent")["data"]

    assert snapshot["quality"]["blocking_sections"] == ["cockpit"]
    assert snapshot["cockpit"]["blocking_dimensions"][0]["dimension"] == (
        "fresh_market_add_signal"
    )
    assert snapshot["trade_plans"]["decision_boundary"] == {
        "status": "pricing_only",
        "blocking_dimensions": ["fresh_market_add_signal"],
        "blocked_actions": ["add_positions", "new_positions"],
        "auto_execution": False,
    }
    assert snapshot["quality"]["optional_source_degradations"] == [
        "quotes:fuyao_aicubes",
    ]
    assert snapshot["status"] == "degraded"


def test_unknown_cash_blocks_additions_but_keeps_reduction_preview():
    account_plan = {
        "status": "complete", "preview_only": True,
        "blockers": ["cash_unknown"],
        "alternatives": [{"action": "reduce", "symbol": "600001", "quantity": 100}],
    }
    with patch("application.scheduled_snapshot.build_account_preview",
               return_value=account_plan):
        snapshot = build_service().read(owner_id="scheduled-agent")["data"]

    policy = snapshot["trade_plans"]["cash_policy"]
    assert policy["new_or_add_positions_allowed"] is False
    assert policy["conservative_reductions_allowed"] is True
    assert snapshot["trade_plans"]["portfolio_risk"]["alternatives"][0]["action"] == "reduce"


def test_post_close_failure_is_fail_closed():
    evening = NOW.replace(hour=21, minute=0)
    snapshot = build_service(
        clock=lambda: evening,
        job_runs_reader=lambda **_kwargs: [{
            "job_name": "eod_outcomes", "started_at": evening.isoformat(),
            "status": "error", "error": "password=must-not-leak",
        }],
    ).read(owner_id="scheduled-agent")["data"]
    assert snapshot["post_close_review"]["status"] == "degraded"
    assert snapshot["strategy_adjustment_proposals"]["status"] == "degraded"
    assert snapshot["strategy_adjustment_proposals"]["guardrails"]["auto_apply"] is False
    assert "must-not-leak" not in str(snapshot)


def test_route_requires_exact_scheduled_capability():
    from webui import access_control
    from webui.api_server import app

    token = "Bearer " + "x" * 40
    capabilities = set()

    class Authenticator:
        def authenticate(self, authorization):
            if authorization != token:
                raise ValueError("invalid")
            return SimpleNamespace(
                agent_id="scheduled-agent", owner_app="shadow-platform", audience="foliant",
                scopes=frozenset({"stock.portfolio.read"}), capabilities=frozenset(capabilities),
            )

    access_control._agent_authenticator = Authenticator()
    fake_result = {"summary": "ok", "resource_uri": "shadow://foliant/reports/scheduled-test",
                   "status": "complete", "provenance": {}, "warnings": [], "data": {}}
    with TestClient(app, base_url="https://stock.example.com") as client:
        denied = client.get("/api/machine/v1/agent/scheduled-snapshot",
                            headers={"Authorization": token})
        assert denied.status_code == 403
        capabilities.add("foliant.scheduled-report.read")
        with patch("application.scheduled_snapshot.ScheduledSnapshotService.read",
                   return_value=fake_result) as read:
            allowed = client.get("/api/machine/v1/agent/scheduled-snapshot",
                                 headers={"Authorization": token})
            assert allowed.status_code == 200
            read.assert_called_once_with(owner_id="scheduled-agent")
            fake_result["data"] = {"padding": "x" * 300000}
            post_close_sized = client.get("/api/machine/v1/agent/scheduled-snapshot",
                                          headers={"Authorization": token})
            assert post_close_sized.status_code == 200
            assert post_close_sized.json()["data"] == fake_result["data"]


def test_cli_missing_config_is_structured_and_does_not_call_http(monkeypatch):
    from scripts import foliant_scheduled_snapshot as cli

    for name in ("FOLIANT_AGENT_BASE_URL", "FOLIANT_AGENT_TOKEN_FILE", "FOLIANT_AGENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with patch.object(cli.requests, "get") as get:
        result = cli.fetch_snapshot()
    assert result["status"] == "missing"
    assert result["error"]["code"] == "agent_base_url_missing"
    get.assert_not_called()


def test_cli_auth_failure_and_notification_never_leak_secrets(monkeypatch):
    from notify import notification_router
    from scripts import foliant_scheduled_snapshot as cli

    monkeypatch.setenv("FOLIANT_AGENT_BASE_URL", "https://private.example.invalid")
    monkeypatch.setenv("FOLIANT_AGENT_TOKEN", "super-secret-bearer")
    response = SimpleNamespace(status_code=403)
    with patch.object(cli.requests, "get", return_value=response):
        failure = cli.fetch_snapshot()
    rendered = str(failure)
    assert "private.example.invalid" not in rendered
    assert "super-secret-bearer" not in rendered

    monkeypatch.setenv("QQ_WEBHOOK_URL", "https://private.example.invalid/secret-hook")
    snapshot = selection()
    snapshot.update({
        "schema_version": "scheduled-agent-snapshot-v1",
        "status": "degraded", "trading_day": {"date": "2026-09-10", "confirmed": True},
        "as_of": {"captured_at": "2026-09-10T11:30:00+08:00"},
        "quality": {"status": "degraded"},
        "formal_selection": selection()["data"],
        "wencai_reference": {"ready_groups": 5},
        "holdings": {"count": 2, "status": "complete",
                     "error": "Bearer should-not-appear https://secret.invalid"},
        "trade_plans": {"status": "complete", "portfolio_risk": {"summary": "先观察"}},
        "quotes": {"status": "degraded"},
        "post_close_review": {"due": False, "status": "pending"},
        "holdings_review": {"status": "pending"},
        "next_session_plan": {"status": "pending"},
    })
    captured = {}

    def send(_category, title, body, **_kwargs):
        captured.update(title=title, body=body)
        return {"qq": (False, "https://private.example.invalid/secret-hook failed")}

    with patch.object(notification_router, "send", side_effect=send):
        result = cli.send_qq(snapshot)
    assert result == {"requested": True, "sent": False, "channel": "qq",
                      "error_code": "qq_delivery_failed"}
    body = captured["body"]
    assert "private.example.invalid" not in body
    assert "should-not-appear" not in body
    assert "super-secret-bearer" not in body


def test_cli_truncated_snapshot_never_sends_qq(monkeypatch):
    from notify import notification_router
    from scripts import foliant_scheduled_snapshot as cli

    monkeypatch.setenv("FOLIANT_AGENT_BASE_URL", "http://127.0.0.1:8601")
    monkeypatch.setenv("FOLIANT_AGENT_TOKEN", "test-token")
    monkeypatch.setenv("QQ_WEBHOOK_URL", "https://example.invalid/qq")
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"status": "complete", "data": None,
                      "warnings": ["inline result was truncated"]},
    )
    with patch.object(cli.requests, "get", return_value=response):
        failure = cli.fetch_snapshot()
    assert failure["error"]["code"] == "agent_snapshot_truncated"
    with patch.object(notification_router, "send") as send:
        notification = cli.send_qq(failure)
    assert notification["sent"] is False
    assert notification["error_code"] == "snapshot_contract_incomplete"
    send.assert_not_called()


def test_cli_incomplete_post_close_review_never_sends_qq(monkeypatch):
    from notify import notification_router
    from scripts import foliant_scheduled_snapshot as cli

    monkeypatch.setenv("QQ_WEBHOOK_URL", "https://example.invalid/qq")
    snapshot = {
        "schema_version": "scheduled-agent-snapshot-v1", "status": "degraded",
        "trading_day": {"date": "2026-09-16"},
        "formal_selection": {}, "holdings": {}, "trade_plans": {}, "quotes": {},
        "post_close_review": {"due": True, "status": "degraded", "conclusion": "不完整"},
        "holdings_review": {"status": "complete"},
        "next_session_plan": {"status": "complete"},
        "as_of": {"captured_at": "2026-09-16T20:45:00+08:00"},
        "quality": {"status": "degraded"},
    }
    with patch.object(notification_router, "send") as send:
        notification = cli.send_qq(snapshot)
    assert notification["sent"] is False
    assert notification["error_code"] == "post_close_review_incomplete"
    send.assert_not_called()


def test_cli_report_appends_due_post_close_conclusion():
    from scripts import foliant_scheduled_snapshot as cli

    snapshot = {
        "status": "complete",
        "trading_day": {"date": "2026-09-10", "confirmed": True},
        "formal_selection": {"status": "complete", "formal_top15": [], "formal_top5": []},
        "independent_selection": {"status": "missing"},
        "wencai_reference": {"ready_groups": 5},
        "holdings": {"count": 2, "status": "complete"},
        "trade_plans": {
            "status": "complete", "portfolio_risk": {"summary": "继续观察"},
            "cash_policy": {"stock_budget": {
                "status": "complete", "total_budget_cny": 300000,
                "stock_holding_count": 40, "stock_market_value_cny": 184388,
                "excluded_fund_holding_count": 13, "available_cash_cny": 115612,
                "as_of": "2026-09-14T16:15:00+08:00",
            }},
        },
        "post_close_review": {"due": True, "status": "complete",
                              "conclusion": "盘后闭环完成：无异常。"},
        "strategy_adjustment_proposals": {"proposal_count": 1},
    }
    _, body = cli.render_qq_report(snapshot)
    assert "盘后结论：盘后闭环完成：无异常。" in body
    assert "策略调整：1 项待复核；仅生成建议，不自动应用。" in body
    assert "股票 40 只/市值 ¥184,388" in body
    assert "基金排除 13 只；可用 ¥115,612" in body


def _external_cli_bundle():
    return {
        "channel": "codex-external-independent-v1",
        "idempotency_key": "ext-20260915-cli-test",
        "selection_run_id": "formal-run",
        "decision_as_of": "2026-09-15T20:45:00+08:00",
        "market_regime": "risk_off",
        "external_evidence": [],
        "independent_overlay": [
            {"symbol": f"600{i:03d}", "event_adjustment": 0.0,
             "risk_veto": False, "evidence_ids": []}
            for i in range(1, 16)
        ],
        "news_watchlist": [],
        "tuning_proposals": [],
    }


def test_cli_external_bundle_is_strict_and_rejects_private_fields(tmp_path):
    from scripts import foliant_scheduled_snapshot as cli

    path = tmp_path / "external.json"
    value = _external_cli_bundle()
    value["holdings"] = [{"symbol": "000001"}]
    path.write_text(json.dumps(value), encoding="utf-8")
    bundle, failure = cli._load_external_bundle(str(path))
    assert bundle is None
    assert failure["error"]["code"] == "external_bundle_invalid"


def test_cli_external_rejection_preserves_degraded_snapshot_and_can_notify(
    tmp_path, monkeypatch, capsys,
):
    from copy import deepcopy
    from scripts import foliant_scheduled_snapshot as cli

    path = tmp_path / "external.json"
    bundle = _external_cli_bundle()
    path.write_text(json.dumps(bundle), encoding="utf-8")
    original = build_service().read(owner_id="scheduled-agent")["data"]
    original["external_independent_research"] = {
        "status": "complete", "top15": [{"symbol": "600001"}],
        "top5": [{"symbol": "600001"}], "evidence": [{"dedupe_key": "old"}],
    }
    original["cockpit"] = {"portfolio_policy": {
        "fail_closed": True,
        "market_add_signal": {"source_failure_code": "a500_constituents_missing"},
    }}
    events = []
    failure = cli._failure(
        "external_evidence_dedupe_conflict",
        cli.EXTERNAL_REJECTION_HINTS["external_evidence_dedupe_conflict"],
        status="degraded",
    )
    monkeypatch.setattr(cli, "submit_external_bundle", lambda _bundle: (
        events.append("submit") or None, failure,
    ))
    monkeypatch.setattr(cli, "fetch_snapshot", lambda: (
        events.append("fetch") or deepcopy(original)
    ))
    monkeypatch.setattr(cli, "claim_external_notification", lambda *_args: (
        events.append("claim") or None
    ))
    monkeypatch.setattr(cli, "send_qq", lambda snapshot: (
        events.append("qq") or {"requested": True, "sent": True, "channel": "qq"}
    ))

    assert cli.main(["--external-bundle", str(path), "--send-qq",
                     "--notification-slot", "10:15"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert events == ["submit", "fetch", "qq"]
    assert result["status"] == "degraded"
    assert result["quality"]["status"] == "degraded"
    assert result["external_submission"]["error_code"] == "external_evidence_dedupe_conflict"
    assert result["external_independent_research"]["top5"] == []
    assert result["external_independent_research"]["status"] == "degraded"
    assert result["source_comparison"]["availability"]["external_independent"] is False
    for section in ("formal_selection", "holdings", "trade_plans"):
        assert result[section] == original[section]
    assert result["notification"]["sent"] is True
    assert "本次提交未通过" in cli.render_qq_report(result)[1]
    assert "a500_constituents_missing" in cli.render_qq_report(result)[1]
    assert "外部独立：" not in cli.render_qq_report(result)[1]


def test_cli_external_rejection_does_not_send_without_snapshot(tmp_path, monkeypatch, capsys):
    from notify import notification_router
    from scripts import foliant_scheduled_snapshot as cli

    path = tmp_path / "external.json"
    path.write_text(json.dumps(_external_cli_bundle()), encoding="utf-8")
    monkeypatch.setattr(cli, "submit_external_bundle", lambda _bundle: (
        None, cli._failure("external_evidence_dedupe_conflict", "versioned key", status="degraded"),
    ))
    monkeypatch.setattr(cli, "fetch_snapshot", lambda: cli._failure(
        "agent_unreachable", "agent unavailable", status="degraded",
    ))
    monkeypatch.setenv("QQ_WEBHOOK_URL", "https://example.invalid/qq")
    with patch.object(notification_router, "send") as send:
        assert cli.main(["--external-bundle", str(path), "--send-qq"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "agent_unreachable"
    assert result["external_submission"]["error_code"] == "external_evidence_dedupe_conflict"
    assert result["notification"]["sent"] is False
    send.assert_not_called()


def test_cli_external_post_preserves_public_rejection_code(monkeypatch):
    from scripts import foliant_scheduled_snapshot as cli

    monkeypatch.setenv("FOLIANT_AGENT_BASE_URL", "http://127.0.0.1:8601")
    monkeypatch.setenv("FOLIANT_EXTERNAL_RESEARCH_TOKEN", "test-writer-token")
    response = SimpleNamespace(
        status_code=409,
        json=lambda: {"error": {"code": "external_evidence_dedupe_conflict",
                                "message": "private server text"}},
    )
    with patch.object(cli.requests, "post", return_value=response):
        result, failure = cli.submit_external_bundle({"channel": "test"})
    assert result is None
    assert failure["error"]["code"] == "external_evidence_dedupe_conflict"
    assert "private server text" not in str(failure)


def test_cli_submits_external_before_snapshot_and_claims_only_one_qq(tmp_path, monkeypatch, capsys):
    from copy import deepcopy
    from scripts import foliant_scheduled_snapshot as cli

    path = tmp_path / "external.json"
    bundle = _external_cli_bundle()
    path.write_text(json.dumps(bundle), encoding="utf-8")
    overlay = {
        "overlay_id": "eio_" + "a" * 40,
        "idempotency_key": bundle["idempotency_key"],
        "selection_run_id": bundle["selection_run_id"],
    }
    submission = {"status": "complete", "data": {"overlay": overlay}}
    snapshot = {
        "schema_version": "scheduled-agent-snapshot-v1", "status": "complete",
        "trading_day": {"date": "2026-09-15", "status": "complete"},
        "external_independent_research": {
            "status": "complete", **overlay,
            "decision_as_of": bundle["decision_as_of"],
            "ranking_locked_at": "2026-09-15T20:45:01+08:00",
        },
    }
    events = []
    monkeypatch.setattr(cli, "submit_external_bundle", lambda value: (
        events.append(("submit", value["idempotency_key"])) or submission, None
    ))
    monkeypatch.setattr(cli, "fetch_snapshot", lambda: (
        events.append(("fetch", None)) or deepcopy(snapshot)
    ))
    monkeypatch.setattr(cli, "claim_external_notification", lambda *_args: (
        events.append(("claim", _args[2])) or {"data": {"should_send": True}}, None
    ))
    monkeypatch.setattr(cli, "send_qq", lambda _snapshot: (
        events.append(("qq", None)) or {"requested": True, "sent": True, "channel": "qq"}
    ))
    monkeypatch.setattr(cli, "record_external_notification_delivery", lambda *_args, **_kwargs: (
        events.append(("delivery", _kwargs["sent"])) or
        {"data": {"delivery_status": "delivered"}}, None
    ))

    assert cli.main([
        "--external-bundle", str(path), "--send-qq",
        "--notification-slot", "20:45",
    ]) == 0
    assert [name for name, _ in events] == ["submit", "fetch", "claim", "qq", "delivery"]
    assert events[2][1] == "2026-09-15T20:45+08:00"
    first_output = json.loads(capsys.readouterr().out)
    assert first_output["notification"]["sent"] is True
    assert first_output["notification"]["notification_slot"] == (
        "2026-09-15T20:45+08:00"
    )

    events.clear()
    monkeypatch.setattr(cli, "claim_external_notification", lambda *_args: (
        events.append(("claim", None)) or {"data": {
            "should_send": False, "prior_sent": True, "delivery_status": "delivered",
            "delivered_at": "2026-09-15T20:45:02+08:00",
        }}, None
    ))
    assert cli.main([
        "--external-bundle", str(path), "--send-qq",
        "--notification-slot", "20:45",
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert [name for name, _ in events] == ["submit", "fetch", "claim"]
    assert output["notification"]["prior_sent"] is True
    assert output["notification"]["sent"] is True
    assert output["notification"]["replayed"] is True
    assert output["notification"]["notification_slot"] == "2026-09-15T20:45+08:00"


def test_cli_notification_slot_boundaries_cover_all_four_planned_times():
    from scripts import foliant_scheduled_snapshot as cli

    snapshot = {"trading_day": {"date": "2026-09-16"}}
    shanghai = ZoneInfo("Asia/Shanghai")
    cases = (
        ((10, 14), None),
        ((10, 15), "2026-09-16T10:15+08:00"),
        ((11, 24), "2026-09-16T10:15+08:00"),
        ((11, 25), "2026-09-16T11:25+08:00"),
        ((14, 35), "2026-09-16T14:35+08:00"),
        ((20, 45), "2026-09-16T20:45+08:00"),
        ((23, 59), "2026-09-16T20:45+08:00"),
    )
    for (hour, minute), expected in cases:
        assert cli.scheduled_notification_slot(
            snapshot,
            now=datetime(2026, 9, 16, hour, minute, tzinfo=shanghai),
        ) == expected


def test_cli_absolute_path_from_external_cwd_sends_qq(tmp_path):
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    marker = tmp_path / "qq-post.json"
    (hook_dir / "sitecustomize.py").write_text(
        """
import json
import os
import requests

class Response:
    status_code = 200
    def __init__(self, payload):
        self._payload = payload
    def json(self):
        return self._payload

def fake_get(*_args, **_kwargs):
    return Response({"data": {
        "schema_version": "scheduled-agent-snapshot-v1",
        "status": "degraded",
        "trading_day": {"date": "2026-09-10", "confirmed": False},
        "formal_selection": {"status": "complete", "formal_top15": [], "formal_top5": []},
        "wencai_reference": {"ready_groups": 0},
        "holdings": {"status": "complete", "count": 2},
        "trade_plans": {"status": "degraded", "portfolio_risk": {}},
        "quotes": {"status": "degraded"},
        "post_close_review": {"due": False, "status": "pending"},
        "holdings_review": {"status": "pending"},
        "next_session_plan": {"status": "pending"},
        "as_of": {"captured_at": "2026-09-10T11:30:00+08:00"},
        "quality": {"status": "degraded"},
    }})

def fake_post(_url, *, json=None, **_kwargs):
    with open(os.environ["FOLIANT_TEST_POST_MARKER"], "w", encoding="utf-8") as handle:
        handle.write(__import__("json").dumps(json, ensure_ascii=False))
    return Response({"ok": True})

requests.get = fake_get
requests.post = fake_post
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(hook_dir),
        "FOLIANT_TEST_POST_MARKER": str(marker),
        "FOLIANT_AGENT_BASE_URL": "https://agent.example.invalid",
        "FOLIANT_AGENT_TOKEN": "external-cwd-test-token-that-is-long-enough",
        "QQ_WEBHOOK_URL": "https://qq.example.invalid/private-hook",
        "SHADOW_LOG_TIMESTAMPS": "false",
    })
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "foliant_scheduled_snapshot.py"),
         "--send-qq"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["notification"] == {"requested": True, "sent": True, "channel": "qq"}
    assert json.loads(marker.read_text("utf-8"))["msgtype"] == "markdown"
    assert "external-cwd-test-token" not in completed.stdout
