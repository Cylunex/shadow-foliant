"""Primary A-share selector driven exclusively by local point-in-time snapshots.

Wencai may be supplied as a reference set for comparison, but it cannot add a
candidate, change a score, satisfy a hard gate or rescue an incomplete local run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from data.research_store import ResearchStore


def _number(value) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _first(row: pd.Series, names: Sequence[str]) -> Optional[float]:
    for name in names:
        if name in row.index:
            value = _number(row.get(name))
            if value is not None:
                return value
    return None


def _percentile(series: pd.Series, *, higher_is_better: bool = True,
                missing: float = 0.20) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    valid = numeric.notna()
    result = pd.Series(missing, index=series.index, dtype=float)
    if valid.any():
        ranks = numeric[valid].rank(method="average", pct=True)
        result.loc[valid] = ranks if higher_is_better else (1.0 - ranks + 1.0 / valid.sum())
    return result.clip(0.0, 1.0)


def _bounded(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _trading_return(values: np.ndarray, days: int) -> Optional[float]:
    if len(values) <= days or values[-days - 1] <= 0:
        return None
    return float(values[-1] / values[-days - 1] - 1.0)


def _ma_slope(values: np.ndarray, window: int, lag: int = 10) -> Optional[float]:
    if len(values) < window + lag:
        return None
    current = float(np.mean(values[-window:]))
    previous = float(np.mean(values[-window - lag:-lag]))
    return (current / previous - 1.0) if previous else None


def _max_drawdown(values: np.ndarray) -> float:
    peaks = np.maximum.accumulate(values)
    return float(np.min(values / peaks - 1.0)) if len(values) else -1.0


def _normalize_reference(items: Optional[Iterable[object]]) -> List[dict]:
    output: Dict[str, dict] = {}
    for item in items or ():
        if isinstance(item, dict):
            raw = item.get("symbol") or item.get("code") or item.get("股票代码")
            labels = item.get("source_labels") or item.get("sources") or item.get("src") or []
        else:
            raw, labels = item, []
        symbol = "".join(ch for ch in str(raw or "") if ch.isdigit())[-6:]
        if len(symbol) != 6:
            continue
        if isinstance(labels, str):
            labels = [labels]
        current = output.setdefault(symbol, {"symbol": symbol, "source_labels": []})
        for label in labels:
            text = str(label).strip()
            if text and text not in current["source_labels"]:
                current["source_labels"].append(text)
    return list(output.values())


@dataclass(frozen=True)
class SelectionPolicy:
    fundamental_top_n: int = 200
    technical_top_n: int = 50
    diversified_top_n: int = 20
    final_n: int = 15
    min_history_days: int = 60
    preferred_history_days: int = 400
    max_per_industry: int = 3
    min_warehouse_coverage: float = 0.80

    @classmethod
    def from_env(cls) -> "SelectionPolicy":
        return cls(
            fundamental_top_n=max(20, int(os.getenv("LOCAL_SELECTION_FUNDAMENTAL_TOP_N", "200"))),
            technical_top_n=max(10, int(os.getenv("LOCAL_SELECTION_TECHNICAL_TOP_N", "50"))),
            diversified_top_n=max(5, int(os.getenv("LOCAL_SELECTION_DIVERSIFIED_TOP_N", "20"))),
            final_n=max(5, min(15, int(os.getenv("LOCAL_SELECTION_FINAL_N", "15")))),
            min_history_days=max(60, int(os.getenv("LOCAL_SELECTION_MIN_HISTORY_DAYS", "60"))),
            preferred_history_days=max(320, int(os.getenv("LOCAL_SELECTION_PREFERRED_HISTORY_DAYS", "400"))),
            max_per_industry=max(1, int(os.getenv("LOCAL_SELECTION_MAX_PER_INDUSTRY", "3"))),
            min_warehouse_coverage=_bounded(
                float(os.getenv("LOCAL_SELECTION_MIN_WAREHOUSE_COVERAGE", "0.80")), 0.1, 1.0
            ),
        )


class LocalStockSelector:
    def __init__(self, store: Optional[ResearchStore] = None,
                 policy: Optional[SelectionPolicy] = None):
        self.store = store or ResearchStore()
        self.policy = policy or SelectionPolicy.from_env()

    def run(self, selection_date: Optional[str] = None,
            *, wencai_reference: Optional[Iterable[object]] = None,
            persist: bool = True) -> dict:
        selection_date = pd.Timestamp(selection_date or date.today()).date().isoformat()
        universe = self.store.load_universe(selection_date)
        panel = self.store.load_daily_panel(
            selection_date, trading_days=self.policy.preferred_history_days + 20
        )
        reference = _normalize_reference(wencai_reference)
        if universe.empty or panel.empty:
            return self._failed(
                selection_date, "local warehouse is empty", len(universe), reference, persist
            )

        panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
        panel = panel.dropna(subset=["trade_date", "symbol", "close"])
        panel["close"] = pd.to_numeric(panel["close"], errors="coerce")
        panel["volume"] = pd.to_numeric(panel["volume"], errors="coerce")
        market_as_of = panel["trade_date"].max()
        history_counts = panel.groupby("symbol")["trade_date"].nunique()
        current_symbols = set(panel.loc[panel["trade_date"] == market_as_of, "symbol"])
        universe = universe.drop_duplicates("symbol").copy()
        universe["history_days"] = universe["symbol"].map(history_counts).fillna(0).astype(int)
        universe["has_current_bar"] = universe["symbol"].isin(current_symbols)
        coverage = float(universe["has_current_bar"].mean()) if len(universe) else 0.0
        if coverage < self.policy.min_warehouse_coverage:
            return self._failed(
                selection_date, "local warehouse coverage below threshold", len(universe),
                reference, persist, coverage=coverage,
                metadata={"market_as_of": market_as_of.date().isoformat()}
            )

        universe = self._hard_gates(universe, panel, market_as_of)
        if universe.empty:
            return self._failed(
                selection_date, "no securities passed local hard gates", 0,
                reference, persist, coverage=coverage,
            )

        valuations = self.store.load_valuations(selection_date)
        fundamentals = self._fundamentals(selection_date)
        features = self._build_features(universe, panel, market_as_of)
        frame = features
        if not valuations.empty and "symbol" in valuations.columns:
            frame = frame.merge(valuations, on="symbol", how="left", suffixes=("", "_valuation"))
        if not fundamentals.empty and "symbol" in fundamentals.columns:
            frame = frame.merge(fundamentals, on="symbol", how="left")
        frame = self._score_fundamentals(frame)
        fundamental_pool = frame.sort_values(
            ["fundamental_score", "fundamental_coverage", "history_coverage"], ascending=False
        ).head(self.policy.fundamental_top_n).copy()

        fundamental_pool = self._score_technical(fundamental_pool)
        technical_pool = fundamental_pool.sort_values(
            ["technical_60_score", "fundamental_score"], ascending=False
        ).head(self.policy.technical_top_n).copy()
        technical_pool = self._score_industry_and_quality(technical_pool, frame)
        technical_pool = self._apply_events(technical_pool, selection_date)
        technical_pool["total_score"] = (
            technical_pool["fundamental_score"] + technical_pool["technical_60_score"]
            + technical_pool["industry_score"] + technical_pool["quality_score"]
            + technical_pool["correction_120"] + technical_pool["correction_250"]
            + technical_pool["event_correction"]
        )
        technical_pool = technical_pool.sort_values(
            ["total_score", "data_coverage", "technical_60_score"], ascending=False
        )
        diversified = self._diversify(technical_pool)
        candidates = self._records(diversified.head(self.policy.final_n))
        comparison = self._comparison(candidates, reference)
        run = {
            "selection_date": selection_date,
            "status": "success" if candidates else "error",
            "universe_count": int(len(history_counts)),
            "eligible_count": int(len(frame)),
            "coverage": coverage,
            "comparison": comparison,
            "metadata": {
                "market_as_of": market_as_of.date().isoformat(),
                "period_semantics": "trading_days",
                "primary_pipeline": "local_pit",
                "reference_affects_score": False,
                "stage_counts": {
                    "fundamental": len(fundamental_pool), "technical": len(technical_pool),
                    "diversified": len(diversified), "final": len(candidates),
                },
            },
        }
        if persist:
            run["run_id"] = self.store.save_selection(run, candidates, reference)
        return {**run, "candidates": candidates, "wencai_reference": reference}

    def _failed(self, selection_date: str, reason: str, universe_count: int,
                reference: List[dict], persist: bool, *, coverage: float = 0.0,
                metadata: Optional[dict] = None) -> dict:
        run = {
            "selection_date": selection_date, "status": "incomplete",
            "universe_count": universe_count, "eligible_count": 0, "coverage": coverage,
            "comparison": self._comparison([], reference),
            "metadata": {"reason": reason, "primary_pipeline": "local_pit",
                         "reference_affects_score": False, **(metadata or {})},
        }
        if persist:
            run["run_id"] = self.store.save_selection(run, [], reference)
        return {**run, "candidates": [], "wencai_reference": reference}

    def _hard_gates(self, universe: pd.DataFrame, panel: pd.DataFrame,
                    market_as_of: pd.Timestamp) -> pd.DataFrame:
        result = universe.copy()
        names = result.get("name", pd.Series("", index=result.index)).fillna("").astype(str)
        statuses = (
            result.get("list_status", pd.Series("L", index=result.index))
            .fillna("L").astype(str).str.strip().str.upper()
        )
        # The normalized store value is L. Keep compatibility with snapshots
        # ingested before normalization, where zzshare encoded listed as 1.
        listed = statuses.isin({"L", "1", "LISTED", "ACTIVE", "TRUE"})
        result = result[listed & ~names.str.upper().str.contains(r"(?:^|\*)ST", regex=True)]
        if os.getenv("EXCLUDE_KCB", "true").lower() not in {"0", "false", "no", "off"}:
            result = result[~result["symbol"].str.startswith(("688", "689"))]
        result = result[result["history_days"] >= self.policy.min_history_days]
        latest = panel[panel["trade_date"] == market_as_of].set_index("symbol")
        result["latest_volume"] = result["symbol"].map(latest["volume"])
        result["latest_close"] = result["symbol"].map(latest["close"])
        result["latest_paused"] = result["symbol"].map(latest["is_paused"]).fillna(0)
        result["latest_st"] = result["symbol"].map(latest["is_st"]).fillna(0)
        return result[
            result["has_current_bar"] & (result["latest_volume"] > 0)
            & (result["latest_close"] > 0) & (result["latest_paused"] == 0)
            & (result["latest_st"] == 0)
        ].copy()

    def _fundamentals(self, selection_date: str) -> pd.DataFrame:
        tables = {}
        for table in ("indicator", "income", "balance", "cash_flow"):
            frame = self.store.load_financial_pit(table, selection_date)
            if frame.empty:
                continue
            if "pub_date" in frame.columns:
                published = pd.to_datetime(frame["pub_date"], errors="coerce")
                frame = frame[published.notna() & (published <= pd.Timestamp(selection_date))]
            frame = frame.drop_duplicates("symbol", keep="last").set_index("symbol")
            tables[table] = frame
        symbols = sorted(set().union(*(set(frame.index) for frame in tables.values()))) if tables else []
        records = []
        for symbol in symbols:
            record = {"symbol": symbol}
            for table, frame in tables.items():
                if symbol not in frame.index:
                    continue
                row = frame.loc[symbol]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[-1]
                for key, value in row.items():
                    if key not in {"symbol", "as_of", "quality_status"}:
                        record[f"{table}_{key}"] = value
            records.append(record)
        return pd.DataFrame(records) if records else pd.DataFrame(columns=["symbol"])

    def _build_features(self, universe: pd.DataFrame, panel: pd.DataFrame,
                        market_as_of: pd.Timestamp) -> pd.DataFrame:
        meta = universe.set_index("symbol")
        rows = []
        for symbol, bars in panel[panel["symbol"].isin(meta.index)].groupby("symbol"):
            bars = bars.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
            closes = pd.to_numeric(bars["close"], errors="coerce").dropna().to_numpy(dtype=float)
            if len(closes) < self.policy.min_history_days:
                continue
            volumes = pd.to_numeric(bars["volume"], errors="coerce").fillna(0).to_numpy(dtype=float)
            rets = pd.Series(closes).pct_change().dropna()
            ma60 = float(np.mean(closes[-60:]))
            low60, high60 = float(np.min(closes[-60:])), float(np.max(closes[-60:]))
            industry = str(meta.loc[symbol].get("industry") or "未分类")
            rows.append({
                "symbol": symbol, "name": meta.loc[symbol].get("name"), "industry": industry,
                "history_days": len(closes), "close": closes[-1], "ret_60": _trading_return(closes, 60),
                "ma60_slope": _ma_slope(closes, 60), "above_ma60": closes[-1] / ma60 - 1.0,
                "range_pos_60": (closes[-1] - low60) / (high60 - low60) if high60 > low60 else 0.5,
                "max_drawdown_60": _max_drawdown(closes[-60:]),
                "volatility_60": float(rets.tail(60).std() * math.sqrt(252)) if len(rets) >= 20 else None,
                "persistence_60": float((rets.tail(60) > 0).mean()) if len(rets) else None,
                "volume_5_vs_60": float(np.mean(volumes[-5:]) / np.mean(volumes[-60:]))
                    if len(volumes) >= 60 and np.mean(volumes[-60:]) > 0 else None,
                "volume_20_vs_60": float(np.mean(volumes[-20:]) / np.mean(volumes[-60:]))
                    if len(volumes) >= 60 and np.mean(volumes[-60:]) > 0 else None,
                "correction_120": self._medium_correction(closes),
                "correction_250": self._long_correction(closes),
                "state": self._technical_state(closes),
                "history_coverage": min(1.0, len(closes) / self.policy.preferred_history_days),
            })
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        market_return = frame["ret_60"].median(skipna=True)
        industry_return = frame.groupby("industry")["ret_60"].transform("median")
        frame["market_excess_60"] = frame["ret_60"] - market_return
        frame["industry_excess_60"] = frame["ret_60"] - industry_return
        return frame

    @staticmethod
    def _medium_correction(closes: np.ndarray) -> float:
        if len(closes) < 120:
            return 0.0
        ma60, ma120 = float(np.mean(closes[-60:])), float(np.mean(closes[-120:]))
        slope = _ma_slope(closes, 120)
        score = 0.0
        score += 2.0 if closes[-1] >= ma120 else -3.0
        score += 2.0 if ma60 >= ma120 else -3.0
        score += 1.0 if slope is not None and slope >= 0 else -4.0
        return _bounded(score, -10.0, 5.0)

    @staticmethod
    def _long_correction(closes: np.ndarray) -> float:
        if len(closes) < 250:
            return 0.0
        ma250 = float(np.mean(closes[-250:]))
        slope = _ma_slope(closes, 250)
        extension = closes[-1] / ma250 - 1.0
        score = 0.0
        score += 2.0 if closes[-1] >= ma250 else -3.0
        score += 2.0 if slope is not None and slope >= 0 else -5.0
        if extension > 0.45:
            score -= min(4.0, (extension - 0.45) * 10)
        elif -0.10 <= extension <= 0.25:
            score += 1.0
        return _bounded(score, -10.0, 5.0)

    @staticmethod
    def _technical_state(closes: np.ndarray) -> str:
        ma60 = float(np.mean(closes[-60:]))
        slope60 = _ma_slope(closes, 60) or 0.0
        if len(closes) >= 250:
            ma120, ma250 = float(np.mean(closes[-120:])), float(np.mean(closes[-250:]))
            slope120, slope250 = _ma_slope(closes, 120) or 0.0, _ma_slope(closes, 250) or 0.0
            if closes[-1] > ma60 and slope60 > 0 and slope120 >= 0 and slope250 >= 0:
                return "趋势确认"
            if closes[-1] > ma60 and slope60 > 0 and (closes[-1] < ma120 or closes[-1] < ma250):
                return "反转观察"
            if closes[-1] > ma60 and slope120 < 0 and slope250 < 0:
                return "长期弱势反弹"
        return "趋势观察" if closes[-1] > ma60 and slope60 > 0 else "弱势"

    def _score_fundamentals(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        def values(names):
            return out.apply(lambda row: _first(row, names), axis=1)

        roe = values(("indicator_roe", "indicator_roe_weighted", "indicator_roe_ttm"))
        growth = values(("indicator_net_profit_growth", "indicator_inc_net_profit_year_on_year",
                         "income_net_profit_growth", "income_np_parent_company_owners_yoy"))
        liabilities = values(("balance_total_liability", "balance_total_liabilities"))
        assets = values(("balance_total_assets", "balance_asset_total"))
        debt = liabilities / assets.replace(0, np.nan)
        ocf = values(("cash_flow_net_operate_cash_flow", "cash_flow_net_operating_cash_flow"))
        profit = values(("income_np_parent_company_owners", "income_net_profit"))
        cash_quality = ocf / profit.abs().replace(0, np.nan)
        pe = pd.to_numeric(out["pe_ttm"], errors="coerce") if "pe_ttm" in out else pd.Series(np.nan, index=out.index)
        pb = pd.to_numeric(out["pb"], errors="coerce") if "pb" in out else pd.Series(np.nan, index=out.index)
        pe = pe.where(pe > 0)
        pb = pb.where(pb > 0)
        out["fundamental_score"] = (
            _percentile(roe) * 8 + _percentile(growth) * 8
            + _percentile(debt, higher_is_better=False) * 6
            + _percentile(cash_quality.clip(-5, 5)) * 6
            + _percentile(pe, higher_is_better=False) * 7
            + _percentile(pb, higher_is_better=False) * 5
        )
        out["fundamental_coverage"] = pd.concat(
            [roe, growth, debt, cash_quality, pe, pb], axis=1
        ).notna().mean(axis=1)
        return out

    @staticmethod
    def _score_technical(frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["technical_60_score"] = (
            _percentile(out["market_excess_60"]) * 6
            + _percentile(out["industry_excess_60"]) * 5
            + _percentile(out["ma60_slope"]) * 5
            + _percentile(out["persistence_60"]) * 4
            + _percentile(out["max_drawdown_60"]) * 3
            + _percentile(out["volatility_60"], higher_is_better=False) * 2
            + _percentile(out["volume_5_vs_60"]) * 2
            + _percentile(out["volume_20_vs_60"]) * 1
            + _percentile((out["range_pos_60"] - 0.75).abs(), higher_is_better=False) * 2
        )
        return out

    @staticmethod
    def _score_industry_and_quality(frame: pd.DataFrame, full_frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        breadth = full_frame.assign(up=full_frame["ret_60"] > 0).groupby("industry")["up"].mean()
        sector_return = full_frame.groupby("industry")["ret_60"].median()
        out["industry_breadth"] = out["industry"].map(breadth)
        out["industry_return"] = out["industry"].map(sector_return)
        out["industry_score"] = (
            _percentile(out["industry_breadth"]) * 8
            + _percentile(out["industry_return"]) * 7
        )
        valuation_columns = [c for c in ("pe_ttm", "pb") if c in out.columns]
        valuation_present = (out[valuation_columns].notna().mean(axis=1) if valuation_columns
                             else pd.Series(0.0, index=out.index))
        out["data_coverage"] = (
            out["history_coverage"] * 0.45 + out["fundamental_coverage"] * 0.40
            + valuation_present * 0.15
        ).clip(0, 1)
        out["quality_score"] = out["data_coverage"] * 15
        return out

    def _apply_events(self, frame: pd.DataFrame, selection_date: str) -> pd.DataFrame:
        out = frame.copy()
        events = self.store.load_events(selection_date)
        out["event_correction"] = 0.0
        if events.empty:
            return out
        event_days = pd.to_datetime(events["effective_at"], errors="coerce")
        age = (pd.Timestamp(selection_date) - event_days).dt.days.clip(lower=0)
        half_life = events["event_type"].map({
            "regulatory": 60, "fraud": 120, "earnings": 30, "dividend": 20,
            "contract": 30, "shareholder": 45, "lockup": 20,
        }).fillna(14)
        decay = np.power(0.5, age / half_life)
        raw = (pd.to_numeric(events["direction"], errors="coerce").fillna(0)
               * pd.to_numeric(events["confidence"], errors="coerce").fillna(0)
               * decay * np.where(events["official"].astype(bool), 1.0, 0.65))
        # Bad news is intentionally asymmetric: risk correction dominates upside catalysts.
        events = events.assign(weighted=np.where(raw < 0, raw * 25, raw * 12))
        corrections = events.groupby("symbol")["weighted"].sum().clip(-25, 12)
        out["event_correction"] = out["symbol"].map(corrections).fillna(0.0)
        return out

    def _diversify(self, frame: pd.DataFrame) -> pd.DataFrame:
        rows, counts = [], {}
        for _, row in frame.iterrows():
            industry = str(row.get("industry") or "未分类")
            if counts.get(industry, 0) >= self.policy.max_per_industry:
                continue
            rows.append(row)
            counts[industry] = counts.get(industry, 0) + 1
            if len(rows) >= self.policy.diversified_top_n:
                break
        return pd.DataFrame(rows, columns=frame.columns)

    @staticmethod
    def _records(frame: pd.DataFrame) -> List[dict]:
        records = []
        for _, row in frame.iterrows():
            state = str(row.get("state") or "趋势观察")
            reasons = [
                f"60日相对市场强度 {float(row.get('market_excess_60') or 0):+.1%}",
                f"60日相对行业强度 {float(row.get('industry_excess_60') or 0):+.1%}",
                state,
            ]
            records.append({
                "symbol": row["symbol"], "name": row.get("name"),
                "industry": row.get("industry"), "state": state,
                "total_score": round(float(row["total_score"]), 4),
                "fundamental_score": round(float(row["fundamental_score"]), 4),
                "technical_60_score": round(float(row["technical_60_score"]), 4),
                "industry_score": round(float(row["industry_score"]), 4),
                "quality_score": round(float(row["quality_score"]), 4),
                "correction_120": round(float(row["correction_120"]), 4),
                "correction_250": round(float(row["correction_250"]), 4),
                "event_correction": round(float(row["event_correction"]), 4),
                "data_coverage": round(float(row["data_coverage"]), 4),
                "source_labels": ["本地PIT数据仓"], "reasons": reasons,
            })
        return records

    @staticmethod
    def _comparison(primary: Sequence[dict], reference: Sequence[dict]) -> dict:
        local = {item["symbol"] for item in primary}
        external = {item["symbol"] for item in reference}
        union = local | external
        return {
            "overlap": sorted(local & external), "local_only": sorted(local - external),
            "reference_only": sorted(external - local),
            "jaccard": round(len(local & external) / len(union), 6) if union else 0.0,
            "reference_affects_score": False,
        }
