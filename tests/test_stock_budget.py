from application.stock_budget import derive_stock_budget


def test_declared_budget_excludes_funds_and_matches_production_example():
    ordinary_stocks = [f"60{i:04d}" for i in range(1, 40)]
    stock_symbols = ordinary_stocks + ["510999"]
    fund_symbols = ["600999"] + [f"16{i:04d}" for i in range(1, 13)]
    holdings = [
        {"symbol": symbol, "quantity": 100}
        for symbol in ordinary_stocks
    ] + [
        {"symbol": "510999", "quantity": 145388},
    ] + [
        {"symbol": symbol, "quantity": 99999}
        for symbol in fund_symbols
    ]
    quotes = [
        {
            "symbol": symbol, "price": 10 if symbol in ordinary_stocks else 1,
            "freshness": "closing_current", "as_of": "2026-09-14T16:15:00+08:00",
        }
        for symbol in stock_symbols
    ]
    result = derive_stock_budget(
        holdings, quotes,
        {"stock_symbols": set(stock_symbols), "fund_symbols": set(fund_symbols)},
    )

    assert result["status"] == "complete"
    assert result["stock_holding_count"] == 40
    assert result["excluded_fund_holding_count"] == 13
    assert result["stock_market_value_cny"] == 184388
    assert result["available_cash_cny"] == 115612
    assert result["as_of"] == "2026-09-14T16:15:00+08:00"
    # Metadata wins even when the symbols resemble the other asset class.
    assert result["classifications"]["510999"] == "stock"
    assert result["classifications"]["600999"] == "fund_or_etf_or_lof"


def test_incomplete_classification_or_stock_quote_blocks_only_buy_budget():
    result = derive_stock_budget(
        [{"symbol": "600001", "quantity": 100},
         {"symbol": "mystery", "quantity": 100}],
        [{"symbol": "600001", "price": 10, "freshness": "stale_or_missing"}],
        {"stock_symbols": {"600001"}, "fund_symbols": set()},
    )

    assert result["status"] == "blocked"
    assert result["available_cash_cny"] is None
    assert "holding_asset_type_unknown" in result["buy_side_blockers"]
    assert "stock_holding_quote_unavailable" in result["buy_side_blockers"]
    assert result["broker_cash_balance"] is False
    assert result["writeback"] is False


def test_over_budget_clamps_available_cash_to_zero_and_is_explicit():
    result = derive_stock_budget(
        [{"symbol": "600001", "quantity": 40000}],
        [{"symbol": "600001", "price": 10, "freshness": "actionable",
          "as_of": "2026-09-14T14:30:00+08:00"}],
        {"stock_symbols": {"600001"}, "fund_symbols": set()},
    )

    assert result["status"] == "complete"
    assert result["available_cash_cny"] == 0
    assert result["over_budget"] is True
    assert result["over_budget_amount_cny"] == 100000
