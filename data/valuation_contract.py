"""Canonical valuation fields and per-field provenance for same-day fallback."""
from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("Asia/Shanghai")
PRIORITY = {"zzshare": 0, "tushare": 1, "baostock": 2,
            "tencent": 3, "mairui": 4, "moma": 5, "eastmoney": 6}
FIELDS = ("pe_ttm", "pb", "market_cap", "circulating_market_cap", "pe_lyr",
          "ps", "pcf", "dividend_yield", "turnover_ratio")
ALIASES = {"pe_ttm": "pe_ratio", "pb": "pb_ratio", "pe_lyr": "pe_ratio_lyr",
           "ps": "ps_ratio", "pcf": "pcf_ratio"}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def iso_day(value):
    try:
        parsed = pd.Timestamp(str(value))
        if pd.isna(parsed):
            return ""
        if parsed.tzinfo is not None:
            parsed = parsed.tz_convert(TZ)
        return parsed.date().isoformat()
    except (ValueError, TypeError):
        return ""


def closing_timestamp(value, day):
    """A live quote must carry an actual same-day timestamp after the close."""
    try:
        text = str(value)
        stamp = pd.to_datetime(text, format="%Y%m%d%H%M%S") if len(text) == 14 and text.isdigit() else pd.Timestamp(text)
        stamp = stamp.tz_localize(TZ) if stamp.tzinfo is None else stamp.tz_convert(TZ)
        return stamp.isoformat() if (stamp.date().isoformat() == day
                                     and stamp.hour >= 15 and stamp <= datetime.now(TZ)) else ""
    except (ValueError, TypeError):
        return ""


def complete(row):
    # Negative PE is valid for loss-making stocks. PB-only live quotes are useful
    # checkpoints, but must not masquerade as a complete formal valuation row.
    return (number(row.get("pb", row.get("pb_ratio"))) not in (None, 0)
            and number(row.get("pe_ttm", row.get("pe_ratio"))) not in (None, 0))


def merge_snapshot(current, incoming, *, day, provider):
    """Merge compatible fields only; never mix days or downgrade higher-ranked data.

    Market caps use CNY 100 million (亿元), matching documented zzshare units.
    A single composite snapshot is republished, so immutable manifest replay does
    not accidentally combine overlapping provider batches.
    """
    rows = {str(r["symbol"]): deepcopy(r) for r in current}
    for raw in incoming.to_dict("records") if incoming is not None else ():
        date_value = raw.get("provider_effective_as_of") or raw.get("trade_date")
        if iso_day(date_value) != day:
            continue
        symbol = "".join(c for c in str(raw.get("symbol") or raw.get("code") or raw.get("ts_code") or "") if c.isdigit())[-6:]
        if len(symbol) != 6:
            continue
        row = rows.setdefault(symbol, {"symbol": symbol, "trade_date": day,
                                       "provider_effective_as_of": day, "field_sources": {},
                                       "market_cap_unit": "CNY_100M"})
        sources = row.setdefault("field_sources", {})
        for field in FIELDS:
            # Adapters normally normalize these definitions. Explicit mismatches
            # fail closed instead of being overwritten by a stable-provider rank.
            if field in {"market_cap", "circulating_market_cap"} and raw.get("market_cap_unit", "CNY_100M") != "CNY_100M":
                continue
            if field == "pe_ttm" and raw.get("pe_basis", "TTM") != "TTM":
                continue
            value = number(raw.get(field, raw.get(ALIASES.get(field))))
            if value is None or (field in {"market_cap", "circulating_market_cap"} and value <= 0):
                continue
            if field in {"pe_ttm", "pb", "ps", "pcf"} and value == 0:
                continue
            old_source = sources.get(field, {}).get("provider", "unknown")
            if number(row.get(field)) is not None and PRIORITY.get(old_source, 99) < PRIORITY[provider]:
                continue
            row[field] = value
            from data.acquisition_evidence import source_family
            sources[field] = {"provider": provider, "source_family": source_family(provider), "as_of": day,
                              "semantic": field, "unit": "CNY_100M" if "market_cap" in field else "ratio",
                              "observed_at": raw.get("observed_at") or datetime.now(TZ).isoformat()}
        # Ratios with unverified/other definitions are evidence, never PE-TTM inputs.
        evidence = row.setdefault("supplemental", {})
        for field in ("pe_reported", "pe_dynamic", "pcf_ncf_ttm"):
            if number(raw.get(field)) is not None:
                evidence[f"{provider}:{field}"] = raw[field]
    return [rows[key] for key in sorted(rows)]
