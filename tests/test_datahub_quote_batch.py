from __future__ import annotations

from unittest.mock import patch

import datahub


def test_partial_provider_batch_continues_for_missing_fund_and_preserves_batch_time():
    calls = []

    def route(_capability, sources, empty=None, timeout=None):
        name, thunk = sources[0]
        calls.append(name)
        if name == "a_stock":
            return {"600001": {"price": 10.0, "name": "甲"}}
        if name == "eastmoney":
            return {"510300": {"price": 4.2, "name": "ETF"}}
        return empty

    with patch.object(datahub, "_route", side_effect=route), \
            patch.object(datahub, "_name_remember"), \
            patch.object(datahub, "_fuyao_available", return_value=False), \
            patch.object(datahub, "_zzshare_available", return_value=False), \
            patch.object(datahub, "_eltdx_available", return_value=False), \
            patch.object(datahub, "_tdx_python_available", return_value=False), \
            patch.object(datahub, "_easy_tdx_available", return_value=False), \
            patch.object(datahub, "_mairui_available", return_value=False), \
            patch.object(datahub, "_moma_available", return_value=False):
        result = datahub.quotes.__wrapped__(["600001", "510300"])

    assert calls == ["a_stock", "tencent", "eastmoney"]
    assert set(result) == {"600001", "510300"}
    assert all(row.get("retrieved_at") for row in result.values())
    assert all(row.get("quote_time_source") == "batch_retrieved_at"
               for row in result.values())


def test_atomic_quote_sources_remain_available_when_composite_bucket_is_empty():
    calls = []

    def route(_capability, sources, empty=None, timeout=None):
        name, thunk = sources[0]
        calls.append(name)
        if name == "a_stock":
            return {}
        if name == "tencent":
            return {"159516": {"price": 0.666, "name": "ETF"}}
        return empty

    with patch.object(datahub, "_route", side_effect=route), \
            patch.object(datahub, "_name_remember"), \
            patch.object(datahub, "_fuyao_available", return_value=False), \
            patch.object(datahub, "_zzshare_available", return_value=False), \
            patch.object(datahub, "_eltdx_available", return_value=False), \
            patch.object(datahub, "_tdx_python_available", return_value=False), \
            patch.object(datahub, "_easy_tdx_available", return_value=False), \
            patch.object(datahub, "_mairui_available", return_value=False), \
            patch.object(datahub, "_moma_available", return_value=False):
        result = datahub.quotes.__wrapped__(["159516"])

    assert calls == ["a_stock", "tencent"]
    assert result["159516"]["source"] == "tencent"


def test_sina_prefix_maps_shenzhen_lof_without_exchange_collision():
    from data.sources._common import sina_code

    assert sina_code("159516") == "sz159516"
    assert sina_code("160723") == "sz160723"
    assert sina_code("510880") == "sh510880"


def test_fuyao_partial_batch_falls_through_and_priority_is_configurable():
    calls = []

    def route(_capability, sources, empty=None, timeout=None):
        name, thunk = sources[0]
        calls.append(name)
        if name == "fuyao_aicubes":
            return {"600001": {"price": 10.0, "quote_time": "2026-09-14T15:00:00+08:00"}}
        if name == "a_stock":
            return {"510300": {"price": 4.2}}
        return empty

    with patch.dict("os.environ", {"FUYAO_AICUBES_PRIORITY_TIER": "1"}), \
            patch.object(datahub, "_route", side_effect=route), \
            patch.object(datahub, "_name_remember"), \
            patch.object(datahub, "_fuyao_available", return_value=True), \
            patch.object(datahub, "_zzshare_available", return_value=False), \
            patch.object(datahub, "_eltdx_available", return_value=False), \
            patch.object(datahub, "_tdx_python_available", return_value=False), \
            patch.object(datahub, "_easy_tdx_available", return_value=False), \
            patch.object(datahub, "_mairui_available", return_value=False), \
            patch.object(datahub, "_moma_available", return_value=False):
        result = datahub.quotes.__wrapped__(["600001", "510300"])

    assert calls == ["a_stock", "fuyao_aicubes"]
    assert set(result) == {"600001", "510300"}
    assert result["600001"]["quote_time_source"] == "provider"


def test_fuyao_route_stats_expose_only_allowlisted_failure_diagnostics():
    from data.sources import fuyao_aicubes

    datahub._STATS.clear()
    try:
        with patch.object(fuyao_aicubes, "capability_status", return_value={
            "capabilities": {"snapshot": {
                "status": "degraded",
                "code": 5001,
                "failure_category": "upstream_unavailable",
                "http_status": 200,
                "request_id": "request_safe",
                "message": "must not leak",
            }},
        }):
            diagnostic = datahub._failure_diagnostic("quotes", "fuyao_aicubes")
        datahub._record("quotes:fuyao_aicubes", False, .2, diagnostic)
        stats = datahub.source_stats()["quotes:fuyao_aicubes"]
    finally:
        datahub._STATS.clear()

    assert stats["failure_code"] == 5001
    assert stats["failure_category"] == "upstream_unavailable"
    assert stats["http_status"] == 200
    assert stats["request_id"] == "request_safe"
    assert "message" not in stats
    assert "must not leak" not in repr(stats)
