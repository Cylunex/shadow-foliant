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
