from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from application.industry_classification import classify_holdings


def _source(tmp_path, monkeypatch, rows):
    path = tmp_path / "sws.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("股票代码", "计入日期", "行业代码", "更新日期"))
        writer.writerows(rows)
    monkeypatch.setenv("FOLIANT_SW_CLASSIFICATION_CSV", str(path))


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
