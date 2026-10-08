from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from analysis import a500_universe, market_breadth


NOW = datetime(2026, 10, 8, 8, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


def _frame(source_date="2026-09-30", count=500):
    return pd.DataFrame({
        "日期": [source_date] * count,
        "指数代码": ["000510"] * count,
        "成分券代码": [f"{i:06d}" for i in range(count)],
    })


def _calendar(_today, *, inclusive):
    assert not inclusive
    return {"ready": True, "latest_confirmed_open_date": "2026-09-30"}


def test_holiday_first_session_accepts_new_fetch_of_last_confirmed_session():
    cache = {}
    get = cache.get
    set_ = lambda key, value, _ttl: cache.__setitem__(key, value) or True
    result = a500_universe.refresh(now=NOW, calendar_consensus=_calendar,
                                   fetcher=_frame, cache_get=get, cache_set=set_)
    assert result["available"]
    assert result["source_date"] == "2026-09-30"
    assert result["fetched_on"] == "2026-10-08"
    assert len(a500_universe.current(now=NOW, cache_get=get)["codes"]) == 500


def test_yesterday_cache_and_wrong_source_date_fail_closed():
    cache = {a500_universe.CACHE_KEY: {
        "source": "csindex", "index_code": "000510", "fetched_on": "2026-09-30",
        "codes": [f"{i:06d}" for i in range(500)],
    }}
    assert a500_universe.current(now=NOW, cache_get=cache.get)["failure_code"] == \
        "a500_constituents_missing"
    set_ = lambda key, value, _ttl: cache.__setitem__(key, value) or True
    result = a500_universe.refresh(now=NOW, calendar_consensus=_calendar,
                                   fetcher=lambda: _frame("2026-09-29"),
                                   cache_get=cache.get, cache_set=set_)
    assert result["failure_code"] == "a500_constituents_asof_mismatch"
    assert a500_universe.current(now=NOW, cache_get=cache.get)["failure_code"] == \
        "a500_constituents_asof_mismatch"


def test_invalid_count_and_unconfirmed_calendar_do_not_publish():
    cache = {}
    set_ = lambda key, value, _ttl: cache.__setitem__(key, value) or True
    invalid = a500_universe.refresh(now=NOW, calendar_consensus=_calendar,
                                    fetcher=lambda: _frame(count=499),
                                    cache_get=cache.get, cache_set=set_)
    assert invalid["failure_code"] == "a500_constituents_invalid"
    uncertain = a500_universe.refresh(
        now=NOW, calendar_consensus=lambda *_args, **_kwargs: {"ready": False},
        fetcher=lambda: (_ for _ in ()).throw(AssertionError("must not fetch")),
        cache_get=cache.get, cache_set=set_)
    assert uncertain["failure_code"] == "a500_constituents_calendar_unconfirmed"
    assert a500_universe.CACHE_KEY not in cache


def test_fixed_report_self_heals_only_with_verified_universe(monkeypatch):
    codes = [f"{i:06d}" for i in range(500)]
    calls = []
    monkeypatch.setattr(a500_universe, "current", lambda: {"available": False,
                         "failure_code": "a500_constituents_missing"})
    monkeypatch.setattr(a500_universe, "refresh", lambda: calls.append(1) or {
        "available": True, "codes": codes, "source": "csindex",
        "source_date": "2026-09-30", "fetched_at": NOW.isoformat()})
    import datahub
    monkeypatch.setattr(datahub, "quotes", lambda selected: {
        code: {"change_pct": 1} for code in selected})
    result = market_breadth.build(force=True)
    assert calls == [1]
    assert result["available"] and result["covered"] == 500
    assert result["constituents_source_date"] == "2026-09-30"


def test_nonforced_breadth_never_fetches_missing_universe(monkeypatch):
    monkeypatch.setattr(a500_universe, "current", lambda: {"available": False,
                         "failure_code": "a500_constituents_source_failed"})
    monkeypatch.setattr(a500_universe, "refresh", lambda: (_ for _ in ()).throw(
        AssertionError("unexpected upstream request")))
    result = market_breadth.build(force=False)
    assert result["failure_code"] == "a500_constituents_source_failed"
