from __future__ import annotations

from scripts.scheduled_qq_format import MAX_CHARS, MAX_LINES, render


DAY = "2026-10-13"
CAPTURED = DAY + "T10:15:50+08:00"
QUOTE_AT = DAY + "T10:15:20+08:00"


def snapshot():
    formal = [{"symbol": f"00000{i}", "name": f"正式{i}"} for i in range(1, 6)]
    independent = [{"symbol": f"60000{i}", "name": f"独立{i}"} for i in range(1, 6)]
    rows = [{"symbol": row["symbol"], "name": row["name"], "price": 10 + i,
             "freshness": "actionable", "as_of": QUOTE_AT}
            for i, row in enumerate(formal + independent)]
    return {
        "trading_day": {"date": DAY}, "as_of": {"captured_at": CAPTURED},
        "formal_selection": {"status": "complete", "run_id": "run-1",
                             "market_as_of": DAY, "formal_top5": formal},
        "independent_selection": {"status": "complete", "top5": independent},
        "quotes": {"captured_at": CAPTURED, "rows": rows},
        "trade_plans": {"holding_actions_authority": {"status": "current",
                                                        "critical_alert_gate": "disabled_missing_authoritative_inputs"},
                        "holding_actions": []},
        "post_close_review": {"due": False},
    }


def test_ten_distinct_picks_with_prices_and_six_digit_codes():
    title, body = render(snapshot())
    assert title == "选股与操作｜10-13 10:15行情"
    assert sum(line.endswith("｜正式") for line in body.splitlines()) == 5
    assert sum(line.endswith("｜独立量化") for line in body.splitlines()) == 5
    assert "000001｜正式1｜¥10.00｜正式" in body
    assert "600005｜独立5｜¥19.00｜独立量化" in body
    assert "行情10:15" in body
    assert "external_" not in body
    assert len(body.splitlines()) <= MAX_LINES and len(body) <= MAX_CHARS


def test_quote_and_action_authority_are_separate():
    value = snapshot()
    value["independent_selection"]["top5"][0] = {"symbol": "000001", "name": "正式1"}
    value["quotes"]["rows"][0]["freshness"] = "stale_or_missing"
    value["trade_plans"]["holding_actions"] = [
        {"symbol": "000001", "name": "正式1", "action": "sell",
         "requested_action": "sell", "reason": "当前价触及 trade_plan 止损",
         "price_actionable": True},
        {"symbol": "000002", "name": "正式2", "action": "hold",
         "requested_action": "sell", "reason": "组合保护"},
        {"symbol": "000003", "name": "正式3", "action": "hold"},
    ]
    _, body = render(value)
    assert "000001｜正式1｜现价待核验｜正式+独立量化" in body
    assert "000001｜正式1｜现价待核验｜卖出观察｜触及止损；执行条件待核验" in body
    assert "000002｜正式2｜¥11.00｜受保护观察" in body
    assert "000003｜正式3｜" in body  # selection only; ordinary hold is omitted
    assert body.count("000003｜") == 1


def test_many_actions_end_with_explicit_omission_count():
    value = snapshot()
    value["trade_plans"]["holding_actions"] = [
        {"symbol": f"300{i:03d}", "name": "风险观察", "action": "reduce",
         "reason": "组合风险"} for i in range(45)
    ]
    _, body = render(value)
    assert "另有" in body and "条未展示" in body
    assert len(body.splitlines()) <= MAX_LINES and len(body) <= MAX_CHARS
    assert body.splitlines()[-1].startswith("另有")


def test_direct_sell_requires_complete_execution_evidence():
    value = snapshot()
    value["trade_plans"]["holding_actions_authority"]["critical_alert_gate"] = "ready"
    value["trade_plans"]["holding_actions"] = [{
        "symbol": "000001", "name": "正式1", "action": "sell",
        "requested_action": "sell", "reason": "当前价触及 trade_plan 止损",
        "price_actionable": True, "quote_time_source": "provider",
        "quote_as_of": QUOTE_AT, "price": 10,
        "quote_provider": "test-provider", "plan_available": True,
        "plan_adjustment": "qfq", "quote_adjustment": "qfq",
        "price_basis": "trade_plan qfq", "selection_run_id": "run-1",
        "plan_as_of": DAY, "tradeable": True,
        "sellability_source": "broker", "sellable_quantity": 100,
        "sellability_as_of": QUOTE_AT, "plan_valid_until": DAY + "T15:00:00+08:00",
    }]
    _, body = render(value)
    assert "000001｜正式1｜¥10.00｜卖出｜触及止损" in body
    value["trade_plans"]["holding_actions"][0]["sellability_source"] = ""
    _, body = render(value)
    assert "000001｜正式1｜¥10.00｜卖出观察｜触及止损；执行条件待核验" in body
