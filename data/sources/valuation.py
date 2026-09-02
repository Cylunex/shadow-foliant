"""Dated valuation adapters. No cross-source routing or database writes here."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from data.source_contracts import source_call
from data.valuation_contract import TZ, closing_timestamp, iso_day, number


def historical(provider, day):
    if provider == "zzshare":
        from .zzshare import get_valuation
        return get_valuation(day)
    from . import tushare
    if not tushare.available():
        return pd.DataFrame()
    # daily_basic has a published 6000-row limit; request two bounded pages when
    # necessary instead of treating a full first page as the entire universe.
    frames = []
    for offset in (0, 6000):
        with source_call("tushare", "valuation"):
            frame = tushare._pro().daily_basic(
                trade_date=day.replace("-", ""), offset=offset, limit=6000,
                fields="ts_code,trade_date,pe,pe_ttm,pb,ps_ttm,dv_ttm,total_mv,circ_mv,turnover_rate",
            )
        if frame is None or frame.empty:
            break
        frame = frame.copy()
        frame["trade_date"] = frame["trade_date"].map(iso_day)
        frame["market_cap"] = pd.to_numeric(frame["total_mv"], errors="coerce") / 10000
        frame["circulating_market_cap"] = pd.to_numeric(frame["circ_mv"], errors="coerce") / 10000
        frames.append(frame.rename(columns={"pe": "pe_lyr", "ps_ttm": "ps",
                                            "dv_ttm": "dividend_yield", "turnover_rate": "turnover_ratio"}))
        if len(frame) < 6000:
            break
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    result.attrs["provenance"] = {"provider": provider, "quality_status": "ok"}
    return result


def live(provider, codes, day):
    # Never ask a live endpoint to manufacture yesterday's snapshot.
    now = datetime.now(TZ)
    if now.date().isoformat() != day or now.hour < 15:
        return pd.DataFrame()
    if provider in {"mairui", "moma"}:
        from . import mairui, moma
        module = mairui if provider == "mairui" else moma
        if not module.available():
            return pd.DataFrame()
        raw = module._source.api.get("realtime", ["hsrl", "ssjy_more"],
                                     {"stock_codes": ",".join(codes)})
        rows = []
        for item in raw:
            code = str(item.get("dm") or "")
            stamp = closing_timestamp(item.get("t"), day)
            if code not in codes or not stamp:
                continue
            rows.append({"symbol": code, "trade_date": day, "observed_at": stamp,
                         "pb": number(item.get("pb_ratio", item.get("sjl"))),
                         "pe_reported": number(item.get("pe"))})
    else:
        from . import tencent, eastmoney
        module = tencent if provider == "tencent" else eastmoney
        with source_call(provider, "valuation"):
            quotes = module.quotes(codes) if provider == "tencent" else module.ulist_quote(codes)
        rows = []
        for code, item in quotes.items():
            stamp = closing_timestamp(item.get("quote_time"), day)
            if code not in codes or not stamp:
                continue
            rows.append({"symbol": code, "trade_date": day, "observed_at": stamp,
                         "pb": number(item.get("pb")),
                         "pe_reported": number(item.get("pe_ttm")),
                         # Tencent's legacy pe_static mapping is not a verified
                         # LYR contract; do not promote it into the PIT warehouse.
                         "market_cap": number(item.get("mcap_yi")),
                         "circulating_market_cap": number(item.get("float_mcap_yi"))})
    return pd.DataFrame(rows)
