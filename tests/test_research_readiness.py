from unittest.mock import Mock

import pandas as pd
import pytest

from data.research_readiness import resolve_valuation, valuation_lag_budget


def make_store(day="2026-08-31", *, coverage=10):
    store = Mock()
    store.latest_valuation_as_of.return_value = day
    store.load_valuations.return_value = pd.DataFrame({
        "symbol": [str(i) for i in range(coverage)],
        "trade_date": [day] * coverage,
        "provider_effective_as_of": [day] * coverage,
        "quality_status": ["ok"] * coverage,
    })
    store.stale_trading_days.return_value = 1
    store.calendar_consensus.return_value = {
        "ready": True, "latest_confirmed_open_date": "2026-09-01",
    }
    store.trade_days_through.return_value = ["2026-08-28", "2026-08-31", "2026-09-01"]
    return store


def resolve(store, *, day="2026-09-01", max_lag=1):
    return resolve_valuation(store, day, map(str, range(10)), min_coverage=.70, max_lag=max_lag)


def test_missing_valuation_and_future_date_cannot_rescue_selection():
    store = make_store()
    store.latest_valuation_as_of.return_value = None
    assert not resolve(store)[1]["ready"]
    assert not resolve(make_store("2026-09-02"))[1]["ready"]


def test_quality_date_and_coverage_are_independent_gates():
    assert not resolve(make_store(coverage=6))[1]["ready"]
    for field, value in (("provider_effective_as_of", "2026-08-28"),
                         ("quality_status", "unknown"), ("trade_date", "2026-09-01")):
        store = make_store()
        store.load_valuations.return_value[field] = value
        assert not resolve(store)[1]["ready"]


def test_unknown_calendar_or_missing_day_is_not_one_day_lag():
    store = make_store()
    store.calendar_consensus.return_value["ready"] = False
    assert not resolve(store)[1]["ready"]
    store = make_store()
    store.trade_days_through.return_value = ["2026-08-31"]
    assert not resolve(store)[1]["ready"]
    store = make_store()
    store.stale_trading_days.return_value = 0
    assert not resolve(store)[1]["ready"]


def test_weekend_uses_trading_days_and_keeps_real_date():
    store = make_store("2026-08-28")
    store.calendar_consensus.return_value["latest_confirmed_open_date"] = "2026-08-31"
    store.trade_days_through.return_value = ["2026-08-27", "2026-08-28", "2026-08-31"]
    frame, state = resolve(store, day="2026-08-31")
    assert state["ready"] and state["status"] == "lagged"
    assert set(frame["trade_date"]) == {"2026-08-28"}
    assert not resolve(store, day="2026-08-31", max_lag=0)[1]["ready"]


def test_partial_current_snapshot_does_not_mix_dates():
    store = make_store("2026-09-01", coverage=5)
    _, state = resolve(store)
    assert not state["ready"]
    store.load_valuations.assert_called_once_with("2026-09-01", exact=True)


@pytest.mark.parametrize("value,expected", [("0", 0), ("1", 1), ("20", 1),
                                            ("-1", 0), ("invalid", 0)])
def test_config_can_only_tighten_hard_one_day_limit(monkeypatch, value, expected):
    monkeypatch.setenv("LOCAL_SELECTION_MAX_VALUATION_LAG", value)
    assert valuation_lag_budget() == expected


def test_legacy_policy_keeps_exact_date_semantics_and_hash():
    from dataclasses import asdict
    import hashlib
    import json
    from analysis.local_stock_selector import SelectionPolicy
    old = asdict(SelectionPolicy())
    old.pop("max_valuation_lag")
    policy = SelectionPolicy(**old)
    assert policy.max_valuation_lag == 0
    assert policy.as_dict() == old
    assert policy.policy_hash == hashlib.sha256(
        json.dumps(old, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
