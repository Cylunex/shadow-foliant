from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import json
import pandas as pd
from zoneinfo import ZoneInfo

from application.industry_classification import classify_holdings
from scripts.foliant_scheduled_snapshot import render_qq_report


def _source(tmp_path, monkeypatch, rows):
    path = tmp_path / "sws.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("股票代码", "计入日期", "行业代码", "更新日期"))
        writer.writerows(rows)
    monkeypatch.setenv("FOLIANT_SW_CLASSIFICATION_CSV", str(path))


def _history(day: str, return_60d: float, return_20d: float):
    dates = pd.date_range(end=day, periods=61, freq="D")
    start = 100.0
    middle = start * (1 + return_60d / 100) / (1 + return_20d / 100)
    first = [start * (middle / start) ** (index / 40) for index in range(41)]
    second = [middle * ((middle * (1 + return_20d / 100)) / middle) ** (index / 20)
              for index in range(1, 21)]
    frame = pd.DataFrame({"Close": first + second}, index=dates)
    frame.attrs.update({"datahub_source": "fresh_cache", "datahub_stale": False,
                        "datahub_cache_age_days": 0.0})
    return frame


def test_current_classification_is_point_in_time_and_excludes_fund(tmp_path, monkeypatch):
    today = date.today().isoformat()
    _source(tmp_path, monkeypatch, [
        ("000001", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("000001", today, "480301", today + " 10:00:00"),
        ("600519", "2020-01-01", "340401", "2020-01-02 10:00:00"),
    ])
    rows = [{"code": "000001"}, {"code": "600519"}, {"code": "510300"}]
    result = classify_holdings(rows, {
        "000001": "stock", "600519": "stock", "510300": "fund_or_etf_or_lof",
    }, as_of=today)
    assert result["status"] == "complete"
    assert result["coverage"] == 1
    assert result["excluded_fund_count"] == 1
    assert result["rows"][0]["industry_l3_code"] == "480301"
    assert result["rows"][0]["industry_l1_code"] == "48"
    assert result["rows"][0]["industry_l1_name"] == "银行"
    assert result["l1_name_status"] == "complete"
    assert result["industry_groups"][0]["holding_count"] == 1
    assert result["theme_status"] == "missing"
    assert result["pruning_status"] == "blocked"
    assert result["source"]["row_count"] == 3
    assert len(result["source"]["csv_sha256"]) == 64


def test_source_observed_after_decision_blocks_historical_replay(tmp_path, monkeypatch):
    _source(tmp_path, monkeypatch, [
        ("000001", "2020-01-01", "480101", "2020-01-02 10:00:00"),
    ])
    result = classify_holdings([{"code": "000001"}], {"000001": "stock"},
                               as_of=(date.today() - timedelta(days=1)).isoformat())
    assert result["status"] == "missing"
    assert result["reason"] == "sw_classification_source_observed_after_decision"


def test_same_day_future_update_is_not_visible(tmp_path, monkeypatch):
    today = date.today().isoformat()
    _source(tmp_path, monkeypatch, [
        ("000001", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("000001", today, "480301", today + " 23:59:59"),
    ])
    decision = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=5)
    result = classify_holdings([{"code": "000001"}], {"000001": "stock"},
                               as_of=decision.isoformat())
    assert result["rows"][0]["industry_l3_code"] == "480101"


def test_same_day_future_effective_time_is_not_visible(tmp_path, monkeypatch):
    today = date.today().isoformat()
    _source(tmp_path, monkeypatch, [
        ("000001", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("000001", today + " 23:59:59", "480301", today + " 00:00:00"),
    ])
    decision = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=5)
    result = classify_holdings([{"code": "000001"}], {"000001": "stock"},
                               as_of=decision.isoformat())
    assert result["rows"][0]["industry_l3_code"] == "480101"


def test_conflicting_same_effective_date_fails_coverage_gate(tmp_path, monkeypatch):
    today = date.today().isoformat()
    _source(tmp_path, monkeypatch, [
        ("000001", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("000001", "2020-01-01", "480301", "2020-01-03 10:00:00"),
    ])
    result = classify_holdings([{"code": "000001"}], {"000001": "stock"},
                               as_of=today)
    assert result["status"] == "degraded"
    assert result["conflict_symbols"] == ["000001"]
    assert result["industry_coverage_gate"] is False


def test_missing_source_is_observational_not_trade_authority(monkeypatch):
    monkeypatch.delenv("FOLIANT_SW_CLASSIFICATION_CSV", raising=False)
    result = classify_holdings([{"code": "000001"}], {"000001": "stock"},
                               as_of=date.today().isoformat())
    assert result["status"] == "missing"
    assert result["auto_execution"] is False
    assert result["peer_comparison_status"] == "blocked"


def test_manual_concepts_annotate_only_current_stocks(tmp_path, monkeypatch):
    today = date.today().isoformat()
    _source(tmp_path, monkeypatch, [
        ("000001", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("600519", "2020-01-01", "340401", "2020-01-02 10:00:00"),
    ])
    path = tmp_path / "concepts.json"
    path.write_text(json.dumps({
        "schema_version": 1, "observed_at": today + "T00:00:00+08:00",
        "concepts": [{"label": "人工智能", "thscode": "885728.TI",
                      "symbols": ["000001", "510300"]}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("FOLIANT_MANUAL_CONCEPTS_JSON", str(path))
    result = classify_holdings(
        [{"code": "000001"}, {"code": "600519"}, {"code": "510300"}],
        {"000001": "stock", "600519": "stock", "510300": "fund_or_etf_or_lof"},
        as_of=today,
    )
    assert result["theme_status"] == "partial"
    assert result["tagged_stock_count"] == 1
    assert result["theme_source"]["curation"] == "manual_snapshot"
    assert result["theme_groups"][0]["symbols"] == ["000001"]
    assert result["rows"][0]["theme_labels"] == ["人工智能"]
    assert result["rows"][0]["theme_evidence"] == [
        {"label": "人工智能", "thscode": "885728.TI"}]
    assert result["rows"][1]["theme_labels"] == []
    assert result["peer_comparison_status"] == "blocked"
    assert result["auto_execution"] is False


def test_manual_concepts_do_not_backfill_history(tmp_path, monkeypatch):
    path = tmp_path / "concepts.json"
    path.write_text(json.dumps({
        "schema_version": 1, "observed_at": "2026-09-16T18:24:48+08:00",
        "concepts": [{"label": "PCB", "thscode": "885959.TI",
                      "symbols": ["000001"]}],
    }), encoding="utf-8")
    monkeypatch.setenv("FOLIANT_MANUAL_CONCEPTS_JSON", str(path))
    from application.manual_concepts import classify_manual_concepts
    result = classify_manual_concepts(["000001"], as_of="2026-09-15")
    assert result["theme_status"] == "missing"
    assert result["theme_reason"] == "manual_concepts_observed_after_decision"


def test_manual_concepts_reject_malformed_memberships(tmp_path, monkeypatch):
    path = tmp_path / "concepts.json"
    path.write_text(json.dumps({
        "schema_version": 1, "observed_at": "2026-09-16T18:24:48+08:00",
        "concepts": [{"label": "PCB", "thscode": "BK0877",
                      "symbols": ["000001"]}],
    }), encoding="utf-8")
    monkeypatch.setenv("FOLIANT_MANUAL_CONCEPTS_JSON", str(path))
    from application.manual_concepts import classify_manual_concepts
    result = classify_manual_concepts(["000001"], as_of="2026-09-17")
    assert result["theme_status"] == "missing"
    assert result["theme_reason"] == "manual_concepts_item_invalid"


def test_qq_summary_names_industry_groups_without_sending():
    _, body = render_qq_report({
        "trading_day": {"date": "2026-09-16"},
        "portfolio_industry": {
            "stock_count": 2, "excluded_fund_count": 1, "coverage": 1,
            "industry_coverage_gate": True,
            "peer_comparison_status": "complete",
            "pruning_observation_count": 1,
            "industry_groups": [
                {"industry_l1_name": "银行", "holding_count": 2},
            ],
        },
    })
    assert "持仓一级行业（前三）：银行2只" in body
    assert "同行比较可用，弱势观察 1 只（需连续确认，非交易指令）" in body


def test_multi_horizon_peer_comparison_and_pruning_watch_are_traceable(
    tmp_path, monkeypatch,
):
    today = date.today().isoformat()
    rows = [
        ("000001", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("600000", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("601169", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("600519", "2020-01-01", "340401", "2020-01-02 10:00:00"),
        ("000858", "2020-01-01", "340401", "2020-01-02 10:00:00"),
    ]
    _source(tmp_path, monkeypatch, rows)
    histories = {
        "000001": _history(today, 30, 15),
        "600000": _history(today, 10, 5),
        "601169": _history(today, -20, -5),
        "600519": _history(today, 8, 3),
        "000858": _history(today, -3, 1),
    }
    holdings = [{"code": symbol} for symbol in histories]
    result = classify_holdings(
        holdings, {symbol: "stock" for symbol in histories}, as_of=today,
        expected_market_date=today, history_loader=histories.get,
    )

    assert result["peer_comparison_status"] == "complete"
    assert result["peer_comparison_available"] is True
    assert result["pruning_status"] == "complete"
    assert result["industry_peer_group_count"] == 2
    bank = next(group for group in result["peer_groups"] if group["group_id"] == "48")
    assert [row["symbol"] for row in bank["rows"]] == ["000001", "600000", "601169"]
    assert all(len(row["input_hash"]) == 64 for row in bank["rows"])
    assert result["pruning_observations"] == [{
        "symbol": "601169", "group_kind": "industry_l1", "group_id": "48",
        "group_name": "银行", "peer_rank": 3, "peer_count": 3,
        "relative_strength_score": 0.0, "return_20d_pct": -5.0,
        "return_60d_pct": -20.0, "excess_vs_group_median_20d_pct": -10.0,
        "excess_vs_group_median_60d_pct": -30.0, "market_as_of": today,
        "status": "watch_pending_consecutive_confirmation",
        "required_consecutive_snapshots": 2, "trade_action": None,
        "execution_price": None,
    }]
    assert result["peer_methodology"]["single_day_return_used"] is False
    assert result["peer_methodology"]["execution_price_authority"] is False
    assert result["auto_execution"] is False


def test_peer_comparison_distinguishes_partial_data_gap(tmp_path, monkeypatch):
    today = date.today().isoformat()
    _source(tmp_path, monkeypatch, [
        ("000001", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("600000", "2020-01-01", "480101", "2020-01-02 10:00:00"),
        ("601169", "2020-01-01", "480101", "2020-01-02 10:00:00"),
    ])
    histories = {
        "000001": _history(today, 30, 15),
        "600000": _history(today, 10, 5),
        "601169": _history((date.today() - timedelta(days=1)).isoformat(), -20, -5),
    }
    result = classify_holdings(
        [{"code": symbol} for symbol in histories],
        {symbol: "stock" for symbol in histories}, as_of=today,
        expected_market_date=today, history_loader=histories.get,
    )

    assert result["peer_comparison_status"] == "partial"
    assert result["peer_comparison_available"] is True
    assert result["peer_comparison_failure_category"] == "data_gap"
    assert result["peer_data_quality"]["failure_by_symbol"] == {
        "601169": "peer_history_not_current",
    }
    assert result["peer_data_quality"]["missing_multi_member_symbols"] == ["601169"]
