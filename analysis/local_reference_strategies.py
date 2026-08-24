"""Deterministic local equivalents of the five legacy Wencai reference screens.

These screens are attachments only.  They may explain resonance around a formal
candidate, but must never alter the local-pit formal score or rank.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd

from data.research_store import ResearchStore


STRATEGY_RULE_VERSION = "local-reference-v1"


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _market_cap_yi(values: pd.Series) -> pd.Series:
    """Normalize provider-native market cap to 亿元 using dataset-level scale."""
    out = pd.to_numeric(values, errors="coerce").astype(float)
    median = out[out > 0].median()
    if pd.isna(median):
        return out
    if median > 10_000_000:  # 元
        return out / 100_000_000
    if median > 10_000:  # 万元
        return out / 10_000
    return out  # 亿元


def _eligible_board(symbols: pd.Series) -> pd.Series:
    codes = symbols.fillna("").astype(str)
    return ~codes.str.startswith(("300", "301", "688", "689"))


def _records(frame: pd.DataFrame, metrics: Iterable[str], top_n: int) -> list[dict]:
    rows = []
    for rank, (_, row) in enumerate(frame.head(top_n).iterrows(), 1):
        item = {
            "rank": rank,
            "symbol": str(row.get("symbol") or ""),
            "name": row.get("name"),
        }
        for metric in metrics:
            value = row.get(metric)
            if value is None or (isinstance(value, float) and np.isnan(value)):
                item[metric] = None
            elif isinstance(value, (np.floating, np.integer)):
                item[metric] = value.item()
            else:
                item[metric] = value
        rows.append(item)
    return rows


def _result(rows: list[dict], rules: list[str], *, status: Optional[str] = None,
            reason: Optional[str] = None, data_as_of: Optional[str] = None) -> dict:
    actual_status = status or ("ready" if rows else "empty")
    result = {"status": actual_status, "rules": rules, "rows": rows}
    if reason:
        result["reason"] = reason
    if data_as_of:
        result["data_as_of"] = data_as_of
    return result


class LocalReferenceStrategyEngine:
    """Build independent, fail-closed local reference screens from one PIT frame."""

    def __init__(self, store: ResearchStore, *, top_n: int = 5):
        self.store = store
        self.top_n = max(1, int(top_n))

    def run(self, frame: pd.DataFrame, *, market_as_of: str) -> Dict[str, object]:
        base = frame.copy()
        base["market_cap_yi"] = _market_cap_yi(_numeric(base, "market_cap"))
        base["circulating_market_cap_yi"] = _market_cap_yi(
            _numeric(base, "circulating_market_cap")
        )
        base["net_profit_growth_pct"] = _numeric(base, "net_profit_growth_pct")
        base["revenue_growth_pct"] = _numeric(base, "revenue_growth_pct")
        base["debt_ratio"] = _numeric(base, "debt_ratio")
        base["pe_ttm"] = _numeric(base, "pe_ttm")
        base["pb"] = _numeric(base, "pb")
        base["dividend_yield"] = _numeric(base, "dividend_yield")
        base["average_amount_20"] = _numeric(base, "average_amount_20")
        board_ok = _eligible_board(base["symbol"])

        low_price = base[
            board_ok & (_numeric(base, "close") < 10)
            & (base["net_profit_growth_pct"] >= 100)
        ].sort_values(["average_amount_20", "symbol"], ascending=[True, True])

        small_cap = base[
            board_ok & (base["market_cap_yi"] > 0) & (base["market_cap_yi"] <= 50)
            & (base["revenue_growth_pct"] >= 10)
            & (base["net_profit_growth_pct"] >= 100)
        ].sort_values(["market_cap_yi", "symbol"], ascending=[True, True])

        profit_growth = base[
            board_ok & base["symbol"].astype(str).str.startswith(("000", "001", "002", "003"))
            & (base["net_profit_growth_pct"] >= 10)
        ].sort_values(["average_amount_20", "symbol"], ascending=[True, True])

        dividend_available = base["dividend_yield"].notna().mean() >= 0.50
        value_mask = (
            board_ok & base["pe_ttm"].gt(0) & base["pe_ttm"].le(20)
            & base["pb"].gt(0) & base["pb"].le(1.5)
            & base["debt_ratio"].ge(0) & base["debt_ratio"].le(0.30)
        )
        if dividend_available:
            value_mask &= base["dividend_yield"].ge(1)
        value = base[value_mask].sort_values(
            ["circulating_market_cap_yi", "market_cap_yi", "symbol"],
            ascending=[True, True, True], na_position="last",
        )

        strategies = {
            "低价擒牛": _result(
                _records(low_price, ("close", "net_profit_growth_pct", "average_amount_20"), self.top_n),
                ["股价<10元", "净利润同比增长>=100%", "非ST/创业板/科创板", "20日均成交额升序"],
                data_as_of=market_as_of,
            ),
            "小市值": _result(
                _records(small_cap, (
                    "market_cap_yi", "revenue_growth_pct", "net_profit_growth_pct"
                ), self.top_n),
                ["总市值<=50亿元", "营收同比增长>=10%", "净利润同比增长>=100%", "非ST/创业板/科创板"],
                data_as_of=market_as_of,
            ),
            "净利增长": _result(
                _records(profit_growth, ("net_profit_growth_pct", "average_amount_20"), self.top_n),
                ["净利润同比增长>=10%", "深圳主板", "非ST/创业板/科创板", "20日均成交额升序"],
                data_as_of=market_as_of,
            ),
            "低估值": _result(
                _records(value, (
                    "pe_ttm", "pb", "dividend_yield", "debt_ratio",
                    "circulating_market_cap_yi",
                ), self.top_n),
                (["0<PE<=20", "0<PB<=1.5"]
                 + (["股息率>=1%"] if dividend_available else ["股息率字段暂无全市场覆盖（未静默补值）"])
                 + ["资产负债率<=30%", "非ST/创业板/科创板"]),
                status=None if dividend_available else "degraded",
                reason=(None if dividend_available else
                        "估值源暂不提供全市场股息率；当前按PE/PB/负债率生成降级参考"),
                data_as_of=market_as_of,
            ),
            "主力资金": self._main_force(base, market_as_of),
        }
        return {
            "rule_version": STRATEGY_RULE_VERSION,
            "market_as_of": market_as_of,
            "reference_affects_score": False,
            "strategies": strategies,
        }

    def _main_force(self, base: pd.DataFrame, market_as_of: str) -> dict:
        flow = self.store.load_fund_flow_daily(market_as_of, exact=True)
        rules = ["真实主力净流入>0", "50亿<=总市值<=5000亿", "非ST/科创板", "主力净流入降序"]
        if flow.empty:
            return _result(
                [], rules, status="unavailable",
                reason="该交易日尚无本地真实资金流快照；未使用成交量代理",
                data_as_of=market_as_of,
            )
        eligible = base[["symbol", "name", "market_cap_yi"]].drop_duplicates("symbol")
        joined = flow.merge(eligible, on="symbol", how="inner", suffixes=("_flow", ""))
        joined["main_net_inflow"] = _numeric(joined, "main_net_inflow")
        joined["main_net_inflow_ratio"] = _numeric(joined, "main_net_inflow_ratio")
        joined = joined[
            joined["main_net_inflow"].gt(0)
            & joined["market_cap_yi"].between(50, 5000, inclusive="both")
            & ~joined["symbol"].astype(str).str.startswith(("688", "689"))
        ].sort_values(["main_net_inflow", "symbol"], ascending=[False, True])
        if "name" not in joined and "name_flow" in joined:
            joined["name"] = joined["name_flow"]
        incomplete = not flow["quality_status"].astype(str).eq("ok").all()
        return _result(
            _records(joined, (
                "main_net_inflow", "main_net_inflow_ratio", "market_cap_yi",
                "close", "change_pct",
            ), self.top_n),
            rules, status="degraded" if incomplete else None,
            reason="资金流快照覆盖不完整" if incomplete else None,
            data_as_of=str(flow["trade_date"].iloc[0]),
        )
