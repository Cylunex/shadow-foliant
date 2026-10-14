from datetime import datetime

import pytest

from application.daily_review import build_daily_review
from application.market_prices import current_day_price, format_reference_price

NOW = datetime.fromisoformat("2026-10-14T19:55:00+08:00")
DAY = {"date": "2026-10-14", "confirmed": True, "is_trading_day": True}


def build(**overrides):
    kwargs = dict(now=NOW, trading_day=DAY,
        holdings=[{"symbol": "000001", "name": "样例", "quantity": 100, "cost_price": 10}],
        quotes=[{"symbol": "000001", "price": 9, "change_pct": -2,
                 "freshness": "closing_current", "as_of": "2026-10-14T15:00:00+08:00"}],
        formal={"status": "complete", "selection_date": DAY["date"], "formal_top15": []},
        previous={})
    return build_daily_review(**(kwargs | overrides))


def test_missing_formal_plan_and_posterior_do_not_block_available_holdings():
    result = build(formal={"status": "missing"})
    assert result["status"] == "partial"
    assert result["holding_observed_count"] == 1
    assert result["holdings"][0]["holding_pnl_pct"] == -10
    assert result["holdings"][0]["unrealized_pnl_cny"] == -100
    assert result["holdings"][0]["realized_pnl_cny"] is None
    assert result["policy"]["posterior_completeness_required"] is False
    assert result["auto_execution"] is False


def test_missing_cost_is_not_zero_profit_and_missing_quote_only_blocks_one_row():
    result = build(holdings=[{"symbol": "000001", "quantity": 100},
                             {"symbol": "000002", "cost_price": 10, "quantity": 100}])
    assert result["holding_observed_count"] == 1
    assert all(row["unrealized_pnl_cny"] is None for row in result["holdings"])
    assert result["status"] == "partial"


def previous(*, at="2026-10-14T14:35:00+08:00", action="hold"):
    return {"trade_date": at[:10], "generated_at": at,
        "holdings": [{"symbol": "000001", "quote_as_of": at, "action": action,
            "requested_action": "sell", "requested_source": "hard_risk"}],
        "trigger_state": {"000001:stop": {"active": False, "last_alerted_at": at},
                          "000001:target": {"active": False, "last_alerted_at": at}}}


def test_recovered_trigger_is_retained_without_restoring_guarded_sell():
    result = build(previous=previous())
    row = result["holdings"][0]
    assert {event["kind"] for event in row["recorded_triggers"]} == {"stop", "target"}
    operation = result["operations"][0]
    assert operation["action"] == "hold"
    assert operation["requested_action"] == "sell"
    assert operation["protected"] is True
    assert operation["executable"] is False


@pytest.mark.parametrize("at", ["2026-10-13T14:35:00+08:00", "2026-10-14T20:35:00+08:00"])
def test_prior_day_or_future_actions_do_not_become_daily_operations(at):
    assert build(previous=previous(at=at))["operations"] == []


def test_same_day_utc_history_uses_actual_local_instant():
    result = build(previous=previous(at="2026-10-14T06:35:00Z"))
    assert result["recorded_operation_count"] == 1


def test_empty_and_closed_day_are_explicit():
    assert build(holdings=[], quotes=[])["status"] == "missing"
    assert build(trading_day=DAY | {"is_trading_day": False})["status"] == "not_applicable"


@pytest.mark.parametrize("price,at,freshness", [
    (0, "2026-10-14T15:00:00+08:00", "closing_current"),
    (float("nan"), "2026-10-14T15:00:00+08:00", "closing_current"),
    (9, "2026-10-13T15:00:00+08:00", "closing_current"),
    (9, "2026-10-14T20:00:00+08:00", "closing_current"),
    (9, "2026-10-14T15:00:00", "closing_current"),
    (9, "2026-10-14T15:00:00+08:00", "stale_or_missing"),
])
def test_bad_quotes_never_gain_observation_authority(price, at, freshness):
    quote = {"price": price, "as_of": at, "freshness": freshness}
    assert current_day_price(quote, NOW) is None
    assert build(quotes=[{"symbol": "000001", **quote}])["holding_observed_count"] == 0


def test_sub_cent_price_display_and_utc_quotes():
    assert format_reference_price(0.813) == "¥0.813"
    assert format_reference_price(10) == "¥10.00"
    assert current_day_price({"price": 9, "as_of": "2026-10-14T07:00:00Z",
                              "freshness": "closing_current"}, NOW) == 9
