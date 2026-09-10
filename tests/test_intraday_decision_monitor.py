from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from application.services import PortfolioAccessService
from jobs import intraday_decision_monitor as monitor
from jobs import jobs_hub

TZ = ZoneInfo("Asia/Shanghai")


def _formal(day="2026-09-10"):
    return {
        "run_id": "formal-run-1",
        "selection_date": day,
        "metadata": {
            "decision_at": f"{day}T09:45:00+08:00",
            "market_as_of": "2026-09-09",
            "manifest_id": "manifest-1",
        },
        "artifacts": {
            "formal_top15": {"payload": [
                {"code": "600001", "rank": 1},
                {"code": "600002", "rank": 2},
                {"code": "600003", "rank": 3},
            ]},
            "formal_top5": {"payload": [
                {"code": "600002", "rank": 1},
            ]},
            "display_overlay": {"payload": [
                {"code": "600001", "name": "正式甲"},
                {"code": "600002", "name": "正式乙"},
                {"code": "600003", "name": "正式丙"},
            ]},
            "wencai_strategy_runs": {"payload": {
                "reference_affects_membership": False,
                "strategies": {"低估值": {"status": "ready", "picks": [
                    {"symbol": "999999", "name": "外部独有"},
                ]}},
            }},
        },
    }


def _plans():
    base = {
        "available": True,
        "candidate_action": "buy",
        "action": "add",
        "entry_low": 9.5,
        "entry_high": 10.5,
        "stop_loss": 9.0,
        "target_price": 12.0,
        "price_basis": "trade_plan；正式manifest qfq日线",
        "plan_as_of": "2026-09-09",
    }
    return {code: dict(base) for code in ("600001", "600002", "600003", "000001")}


def test_pool_covers_holdings_and_formal_candidates_without_wencai_membership():
    formal = _formal()
    pool = monitor.build_monitor_pool(formal, [
        {"code": "000001", "name": "持仓甲", "quantity": 100, "cost_price": 8},
        {"code": "600002", "name": "重合持仓", "quantity": 100, "cost_price": 9},
    ])
    by_code = {row["symbol"]: row for row in pool}
    assert set(by_code) == {"000001", "600001", "600002", "600003"}
    assert by_code["600002"]["sources"] == ["holding", "formal_top5"]
    assert by_code["600002"]["formal_rank"] == 2
    assert by_code["600003"]["priority"] == "formal_top15_watch"
    assert "999999" not in by_code
    assert by_code["000001"]["selection_run_id"] == "formal-run-1"
    assert by_code["000001"]["selection_as_of"] == "2026-09-10T09:45:00+08:00"


def test_portfolio_agent_result_exposes_complete_persisted_snapshot_without_refresh():
    snapshot = {
        "trade_date": "2026-09-10",
        "generated_at": "2026-09-10T10:05:00+08:00",
        "selection_run_id": "formal-run-1",
        "status": "degraded",
        "data_quality": {"quote_as_of": "2026-09-10T10:04:58+08:00"},
        "holdings": [{"symbol": "000001", "action": "data_insufficient"}],
        "formal_top5": [{"symbol": "600002", "action": "hold"}],
        "formal_top15_watch": [{"symbol": "600003", "action": "hold"}],
        "wencai_reference": {"reference_affects_membership": False},
    }
    result = PortfolioAccessService(
        intraday_snapshot_loader=lambda: {"status": "degraded", "data": snapshot}
    ).intraday_decision()
    assert result["status"] == "degraded"
    assert result["data"] == snapshot
    assert result["provenance"]["run_id"] == "formal-run-1"
    assert result["provenance"]["market_as_of"] == "2026-09-10T10:04:58+08:00"
    assert "失败关闭" in result["warnings"][0]

    complete = PortfolioAccessService(
        intraday_snapshot_loader=lambda: {"status": "success", "data": snapshot}
    ).intraday_decision()
    assert complete["status"] == "complete"


def test_cycle_uses_one_batch_quote_and_statefully_rearms_after_exit_and_cooldown():
    now = datetime(2026, 9, 10, 10, 5, tzinfo=TZ)
    persisted = {
        monitor.SNAPSHOT_KEY: {
            "trade_date": now.date().isoformat(),
            "selection_run_id": "formal-run-1",
            "plans": _plans(),
        },
    }
    quote_calls = []
    alerts = []
    quote_now = [now]

    def load(key):
        return persisted.get(key, {})

    def save(key, value):
        persisted[key] = value

    def quote_loader(codes):
        quote_calls.append(list(codes))
        return {code: {"price": 10.0, "change_pct": 0.2,
                       "quote_time": quote_now[0].strftime("%Y%m%d%H%M%S")} for code in codes}

    kwargs = {
        "formal_loader": lambda: _formal(),
        "holdings_loader": lambda: [
            {"code": "000001", "name": "持仓甲", "quantity": 100, "cost_price": 8}
        ],
        "quote_loader": quote_loader,
        "snapshot_loader": load,
        "snapshot_saver": save,
        "notify_fn": lambda title, body: alerts.append((title, body)),
    }
    first = monitor.run_cycle(now=now, **kwargs)
    assert first["status"] == "success"
    assert len(quote_calls) == 1
    assert set(quote_calls[0]) == {"000001", "600001", "600002", "600003"}
    assert sum("进入买入区" in title for title, _ in alerts) == 3

    quote_now[0] = now + timedelta(minutes=20)
    monitor.run_cycle(now=quote_now[0], **kwargs)
    assert sum("进入买入区" in title for title, _ in alerts) == 3

    def outside_quotes(codes):
        return {code: {"price": 11.0, "change_pct": 0.2,
                       "quote_time": (now + timedelta(minutes=40)).strftime("%Y%m%d%H%M%S")}
                for code in codes}

    monitor.run_cycle(now=now + timedelta(minutes=40), quote_loader=outside_quotes,
                      **{k: v for k, v in kwargs.items() if k != "quote_loader"})
    quote_now[0] = now + timedelta(minutes=80)
    monitor.run_cycle(now=quote_now[0], **kwargs)
    assert sum("进入买入区" in title for title, _ in alerts) == 6


def test_lunch_weekend_and_missing_fixed_plan_skip_without_quotes():
    calls = []
    common = {
        "formal_loader": lambda: _formal(), "holdings_loader": list,
        "quote_loader": lambda codes: calls.append(codes) or {},
        "snapshot_loader": lambda key: {}, "snapshot_saver": lambda key, value: None,
    }
    assert monitor.run_cycle(now=datetime(2026, 9, 10, 12, 0, tzinfo=TZ), **common)["status"] == "skipped"
    assert monitor.run_cycle(now=datetime(2026, 9, 12, 10, 0, tzinfo=TZ), **common)["status"] == "skipped"
    result = monitor.run_cycle(now=datetime(2026, 9, 10, 10, 0, tzinfo=TZ), **common)
    assert result["reason"] == "awaiting_fixed_node_trade_plans"
    assert calls == []


def test_stop_target_and_holding_action_escalation_notify_only_on_crossing():
    now = datetime(2026, 9, 10, 10, 5, tzinfo=TZ)
    persisted = {monitor.SNAPSHOT_KEY: {
        "selection_run_id": "formal-run-1", "plans": _plans(),
    }}
    alerts = []
    prices = {"000001": 10.0, "600001": 10.0, "600002": 10.0, "600003": 10.0}
    quote_now = [now]

    def load(key):
        return persisted.get(key, {})

    def save(key, value):
        persisted[key] = value

    def quotes(codes):
        return {code: {"price": prices[code], "change_pct": 0,
                       "quote_time": quote_now[0].strftime("%Y%m%d%H%M%S")}
                for code in codes}

    kwargs = {
        "formal_loader": lambda: _formal(),
        "holdings_loader": lambda: [{"code": "000001", "quantity": 100, "cost_price": 8}],
        "quote_loader": quotes, "snapshot_loader": load, "snapshot_saver": save,
        "notify_fn": lambda title, body: alerts.append(title),
    }
    monitor.run_cycle(now=now, **kwargs)
    baseline = len(alerts)
    prices["000001"] = 12.5
    prices["600002"] = 8.5
    quote_now[0] = now + timedelta(minutes=20)
    monitor.run_cycle(now=quote_now[0], **kwargs)
    assert alerts.count("盘中提醒：触及止盈") == 1
    assert alerts.count("盘中提醒：触及止损") == 1
    assert alerts.count("盘中提醒：持仓动作升级") == 1
    crossed = len(alerts)
    quote_now[0] = now + timedelta(minutes=40)
    monitor.run_cycle(now=quote_now[0], **kwargs)
    assert len(alerts) == crossed
    assert crossed > baseline


def test_stale_quotes_fail_closed_and_report_quality():
    now = datetime(2026, 9, 10, 11, 20, tzinfo=TZ)
    prior = {"selection_run_id": "formal-run-1", "plans": _plans()}
    saved = {}
    result = monitor.run_cycle(
        now=now,
        formal_loader=lambda: _formal(),
        holdings_loader=lambda: [{"code": "000001", "quantity": 100, "cost_price": 8}],
        quote_loader=lambda codes: {
            code: {"price": 10, "quote_time": "20260910100000"} for code in codes
        },
        snapshot_loader=lambda key: prior if key == monitor.SNAPSHOT_KEY else {},
        snapshot_saver=lambda key, value: saved.update(value),
        notify_fn=lambda title, body: None,
    )
    assert result["status"] == "error"
    assert result["data_quality"]["stale_symbols"]
    assert all(row["action"] == "data_insufficient" for row in result["holdings"])
    assert all(row["price_actionable"] is False for row in result["formal_top5"])
    text = monitor.format_fixed_summary(result, "11:20")
    assert "as-of" in text
    assert "暂不给价" in text
    assert "失败关闭" in text


def test_missing_plan_fails_closed_for_holding_action_and_prices():
    now = datetime(2026, 9, 10, 10, 5, tzinfo=TZ)
    plans = _plans()
    plans["000001"] = {"available": False}
    result = monitor.run_cycle(
        now=now,
        formal_loader=lambda: _formal(),
        holdings_loader=lambda: [{"code": "000001", "quantity": 100, "cost_price": 8}],
        quote_loader=lambda codes: {
            code: {
                "price": 7.0,
                "change_pct": -8,
                "quote_time": now.strftime("%Y%m%d%H%M%S"),
            }
            for code in codes
        },
        snapshot_loader=lambda key: {
            "selection_run_id": "formal-run-1", "plans": plans,
            "ma20": 10, "ma60": 10,
        },
        snapshot_saver=lambda key, value: None,
        notify_changes=False,
    )
    holding = result["holdings"][0]
    assert holding["action"] == "data_insufficient"
    assert monitor._row_price(holding, "price") == "暂不给价"


def test_nine_forty_five_summary_keeps_five_reference_states_and_overlap_bounded():
    results = {
        "主力资金": (True, pd.DataFrame([{"股票代码": "600001", "股票简称": "甲"}]), "ok"),
        "低价擒牛": (False, None, "当日缓存缺失"),
        "低估值": (False, None, "timeout"),
        "小市值": (False, None, "timeout"),
        "净利增长": (False, None, "timeout"),
    }
    text = jobs_hub._format_selection_reference_summary(
        [{"code": "600001", "name": "甲", "price": 10}],
        [{"code": str(i)} for i in range(15)],
        results, {"overlap": ["600001"]}, {"600001": "甲"}, "行情 2026-09-09",
    )
    assert len(text.splitlines()) <= 8
    assert "正式TOP5" in text and "正式TOP15：15只" in text
    assert "与正式TOP15重合：600001 甲" in text
    for name in ("主力资金", "低价擒牛", "低估值", "小市值", "净利增长"):
        assert f"问财参考·{name}" in text
    assert "仅供参考" in text


def test_scheduler_registers_twenty_minute_monitor():
    import inspect

    source = inspect.getsource(jobs_hub.register_default_jobs)
    assert "'intraday_decision_monitor',   'every:20:minutes'" in source
