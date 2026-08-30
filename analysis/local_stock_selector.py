"""Primary A-share selector driven exclusively by local point-in-time snapshots.

Wencai may be supplied as a reference set for comparison, but it cannot add a
candidate, change a score, satisfy a hard gate or rescue an incomplete local run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from data.research_store import ResearchStore
from core.decision_context import DecisionContext, dependency_lock_hash


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
                missing: float = 0.0) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    valid = numeric.notna()
    result = pd.Series(missing, index=series.index, dtype=float)
    if valid.any():
        ranks = numeric[valid].rank(method="average", pct=True)
        result.loc[valid] = ranks if higher_is_better else (1.0 - ranks + 1.0 / valid.sum())
    return result.clip(0.0, 1.0)


def _industry_percentile(series: pd.Series, industries: pd.Series, *,
                         higher_is_better: bool = True) -> pd.Series:
    """Prefer classified industry ranks; fall back to market ranks for tiny groups."""
    market = _percentile(series, higher_is_better=higher_is_better)
    result = market.copy()
    labels = industries.fillna("").astype(str).str.strip()
    for label, index in labels.groupby(labels).groups.items():
        if not label or label == "未分类" or len(index) < 5:
            continue
        result.loc[index] = _percentile(
            series.loc[index], higher_is_better=higher_is_better
        )
    return result


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


def _price_limit_ratio(symbol: str, name: str, trade_date: object) -> float:
    label = str(name or "").upper()
    code = "".join(ch for ch in str(symbol or "") if ch.isdigit())[-6:]
    day = pd.Timestamp(trade_date).date()
    if code.startswith(("4", "8", "92")):
        return 0.30
    if code.startswith(("688", "689")):
        return 0.20
    if code.startswith(("300", "301")) and day >= date(2020, 8, 24):
        return 0.20
    # 沪深主板风险警示股票自 2026-07-06 起由 5% 调整为 10%。创业板、
    # 科创板和北交所先按各自板块规则判断，不能被名称中的 ST 错降为 5%。
    if "ST" in label and day < date(2026, 7, 6):
        return 0.05
    return 0.10


def _limit_price(previous_close: float, ratio: float) -> float:
    return float(
        (Decimal(str(previous_close)) * (Decimal("1") + Decimal(str(ratio))))
        .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def _normalize_reference(items: Optional[Iterable[object]]) -> List[dict]:
    output: Dict[str, dict] = {}
    for item in items or ():
        if isinstance(item, dict):
            raw = item.get("symbol") or item.get("code") or item.get("股票代码")
            labels = item.get("source_labels") or item.get("sources") or item.get("src") or []
            name = item.get("name") or item.get("股票简称")
        else:
            raw, labels, name = item, [], None
        symbol = "".join(ch for ch in str(raw or "") if ch.isdigit())[-6:]
        if len(symbol) != 6:
            continue
        if isinstance(labels, str):
            labels = [labels]
        current = output.setdefault(symbol, {"symbol": symbol, "source_labels": []})
        name_text = str(name).strip() if name is not None else ""
        if name_text.lower() not in ("", "nan", "none", "<na>") and not current.get("name"):
            current["name"] = name_text
        for label in labels:
            text = str(label).strip()
            if text and text not in current["source_labels"]:
                current["source_labels"].append(text)
    return list(output.values())


@dataclass(frozen=True)
class SelectionPolicy:
    version: str = "local-fusion-v2"
    core_rule_version: str = "local-pit-v4"
    fundamental_top_n: int = 200
    technical_top_n: int = 50
    diversified_top_n: int = 20
    final_n: int = 15
    min_history_days: int = 70
    preferred_history_days: int = 400
    max_per_industry: int = 5
    max_pairwise_correlation: float = 0.90
    min_warehouse_coverage: float = 0.80
    min_financial_universe_coverage: float = 0.70
    min_stock_fundamental_metrics: int = 4
    min_valuation_coverage: float = 0.70
    min_listing_trading_days: int = 70
    min_average_amount_20: float = 20_000_000.0
    min_correlation_days: int = 40
    top15_core_floor: int = 8
    top15_satellite_cap: int = 5
    top15_timing_cap: int = 2
    top5_core_floor: int = 3
    top5_satellite_cap: int = 1
    top5_timing_cap: int = 1
    nominations_per_local_strategy: int = 5
    genome_nomination_cap: int = 5
    genome_prefilter_n: int = 250
    genome_min_lane_score: float = 45.0
    priority_main_force: float = 1.0
    priority_low_price_bull: float = 1.0
    priority_value: float = 1.0
    priority_small_cap: float = 0.70
    priority_profit_growth: float = 0.50

    @classmethod
    def from_env(cls) -> "SelectionPolicy":
        base = cls()
        return cls(
            fundamental_top_n=max(20, int(os.getenv("LOCAL_SELECTION_FUNDAMENTAL_TOP_N", "200"))),
            technical_top_n=max(10, int(os.getenv("LOCAL_SELECTION_TECHNICAL_TOP_N", "50"))),
            diversified_top_n=max(5, int(os.getenv("LOCAL_SELECTION_DIVERSIFIED_TOP_N", "20"))),
            final_n=max(5, min(15, int(os.getenv("LOCAL_SELECTION_FINAL_N", "15")))),
            min_history_days=max(base.min_history_days, int(
                os.getenv("LOCAL_SELECTION_MIN_HISTORY_DAYS", "70")
            )),
            preferred_history_days=max(base.preferred_history_days, int(
                os.getenv("LOCAL_SELECTION_PREFERRED_HISTORY_DAYS", "400")
            )),
            max_per_industry=min(base.max_per_industry, max(
                1, int(os.getenv("LOCAL_SELECTION_MAX_PER_INDUSTRY", "5"))
            )),
            max_pairwise_correlation=min(base.max_pairwise_correlation, _bounded(
                float(os.getenv("LOCAL_SELECTION_MAX_PAIRWISE_CORRELATION", "0.90")), 0.5, 1.0
            )),
            min_warehouse_coverage=max(base.min_warehouse_coverage, _bounded(
                float(os.getenv("LOCAL_SELECTION_MIN_WAREHOUSE_COVERAGE", "0.80")), 0.1, 1.0
            )),
            min_financial_universe_coverage=max(base.min_financial_universe_coverage, _bounded(
                float(os.getenv("LOCAL_SELECTION_MIN_FINANCIAL_COVERAGE", "0.70")), 0.1, 1.0
            )),
            min_stock_fundamental_metrics=max(
                base.min_stock_fundamental_metrics,
                min(6, int(os.getenv("LOCAL_SELECTION_MIN_FUNDAMENTAL_METRICS", "4")))
            ),
            min_valuation_coverage=max(base.min_valuation_coverage, _bounded(
                float(os.getenv("LOCAL_SELECTION_MIN_VALUATION_COVERAGE", "0.70")), 0.1, 1.0
            )),
            min_listing_trading_days=max(
                base.min_listing_trading_days,
                int(os.getenv("LOCAL_SELECTION_MIN_LISTING_TRADING_DAYS", "70"))
            ),
            min_average_amount_20=max(
                base.min_average_amount_20,
                float(os.getenv("LOCAL_SELECTION_MIN_AVERAGE_AMOUNT_20", "20000000"))
            ),
            min_correlation_days=max(
                base.min_correlation_days,
                min(60, int(os.getenv("LOCAL_SELECTION_MIN_CORRELATION_DAYS", "40")))
            ),
            top15_core_floor=max(8, min(15, int(
                os.getenv("LOCAL_FUSION_TOP15_CORE_FLOOR", "8")
            ))),
            top15_satellite_cap=max(0, min(5, int(
                os.getenv("LOCAL_FUSION_TOP15_SATELLITE_CAP", "5")
            ))),
            top15_timing_cap=max(0, min(2, int(
                os.getenv("LOCAL_FUSION_TOP15_TIMING_CAP", "2")
            ))),
            top5_core_floor=max(3, min(5, int(
                os.getenv("LOCAL_FUSION_TOP5_CORE_FLOOR", "3")
            ))),
            top5_satellite_cap=max(0, min(1, int(
                os.getenv("LOCAL_FUSION_TOP5_SATELLITE_CAP", "1")
            ))),
            top5_timing_cap=max(0, min(1, int(
                os.getenv("LOCAL_FUSION_TOP5_TIMING_CAP", "1")
            ))),
            nominations_per_local_strategy=max(1, min(5, int(
                os.getenv("LOCAL_FUSION_LOCAL_NOMINATIONS", "5")
            ))),
            genome_nomination_cap=max(0, min(5, int(
                os.getenv("LOCAL_FUSION_GENOME_NOMINATIONS", "5")
            ))),
            genome_prefilter_n=max(50, min(500, int(
                os.getenv("LOCAL_FUSION_GENOME_PREFILTER_N", "250")
            ))),
            genome_min_lane_score=_bounded(float(
                os.getenv("LOCAL_FUSION_GENOME_MIN_SCORE", "45")
            ), 0, 100),
        )

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def policy_hash(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CorrelationResult:
    status: str
    value: Optional[float]
    overlap_days: int


class LocalStockSelector:
    def __init__(self, store: Optional[ResearchStore] = None,
                 policy: Optional[SelectionPolicy] = None):
        self.store = store or ResearchStore()
        self.policy = policy or SelectionPolicy.from_env()
        if policy is None:
            try:
                active = self.store.load_active_strategy_policy()
                payload = dict((active or {}).get("payload") or {})
                priority = dict(payload.get("strategy_priority") or {})
                overrides = {
                    key: payload[key] for key in (
                        "top15_core_floor", "top15_satellite_cap", "top15_timing_cap",
                        "top5_core_floor", "top5_satellite_cap", "top5_timing_cap",
                        "nominations_per_local_strategy", "genome_nomination_cap",
                        "genome_prefilter_n", "genome_min_lane_score",
                        "max_per_industry", "max_pairwise_correlation",
                    ) if key in payload
                }
                overrides.update({
                    "priority_main_force": priority.get("主力资金", self.policy.priority_main_force),
                    "priority_low_price_bull": priority.get("低价擒牛", self.policy.priority_low_price_bull),
                    "priority_value": priority.get("低估值", self.policy.priority_value),
                    "priority_small_cap": priority.get("小市值", self.policy.priority_small_cap),
                    "priority_profit_growth": priority.get("净利增长", self.policy.priority_profit_growth),
                })
                if overrides:
                    self.policy = replace(self.policy, **overrides)
            except Exception:
                # A missing policy table must not make the selector unavailable
                # during a rolling deployment; the versioned defaults remain safe.
                pass
        self._last_correlation_matrix: List[dict] = []

    def run(self, selection_date: Optional[str] = None,
            *, data_cutoff: Optional[str] = None,
            decision_at: Optional[object] = None,
            decision_mode: str = "preopen",
            wencai_reference: Optional[Iterable[object]] = None,
            strategy_snapshot: Optional[dict] = None,
            persist: bool = True, _generation_retry: int = 0) -> dict:
        selection_date = pd.Timestamp(selection_date or date.today()).date().isoformat()
        try:
            context = DecisionContext.build(
                selection_date,
                data_cutoff=data_cutoff,
                decision_at=decision_at,
                mode=decision_mode,
                policy_version=self.policy.version,
                policy_hash=self.policy.policy_hash,
            )
        except ValueError as exc:
            return self._failed(
                selection_date, f"invalid_decision_context:{exc}", 0, [], persist
            )
        cutoff = context.market_cutoff
        reference = _normalize_reference(wencai_reference)
        generation_capabilities = (
            "security_master", "trade_calendar", "daily_market", "valuation",
            "financial_pit", "fund_flow", "events",
        )
        generation_reader = getattr(self.store, "generation_vector", None)
        generation_before = (
            generation_reader(generation_capabilities) if callable(generation_reader) else {}
        )
        pit = self.store.pit_coverage(context.universe_cutoff)
        if pit.get("pit_coverage_start_date") and not pit.get("historical_pit_available"):
            return self._failed(
                selection_date, "historical_pit_unavailable", 0, reference, persist,
                metadata={**pit, "decision_context": context.as_dict()},
            )
        universe = self.store.load_universe(
            context.universe_cutoff, cutoff_at=context.decision_at
        )
        universe_snapshot_id = str(universe.attrs.get("snapshot_id") or "")
        panel = self.store.load_daily_panel(
            cutoff, trading_days=self.policy.preferred_history_days + 20
        )
        if universe.empty or panel.empty:
            return self._failed(
                selection_date, "local warehouse is empty", len(universe), reference, persist,
                metadata={**pit, "decision_context": context.as_dict()},
            )
        pit_universe_count = int(len(universe.drop_duplicates("symbol")))

        panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
        panel = panel.dropna(subset=["trade_date", "symbol", "close"])
        panel["close"] = pd.to_numeric(panel["close"], errors="coerce")
        panel["volume"] = pd.to_numeric(panel["volume"], errors="coerce")
        panel = panel[~panel["quality_status"].isin({"unknown_unit", "failed"})]
        if panel.empty:
            return self._failed(
                selection_date, "local warehouse has no usable normalized bars",
                pit_universe_count, reference, persist,
                metadata={**pit, "data_cutoff": cutoff},
            )
        calendar = self.store.calendar_consensus(
            cutoff, inclusive=context.market_cutoff_inclusive
        )
        expected_market_as_of = calendar.get("latest_confirmed_open_date")
        if not calendar.get("ready") or not expected_market_as_of:
            return self._failed(
                selection_date, "trade calendar consensus unavailable", pit_universe_count,
                reference, persist, metadata={**pit, "decision_context": context.as_dict(),
                                              "calendar_consensus": calendar},
            )
        market_as_of = panel["trade_date"].max()
        actual_market_as_of = market_as_of.date().isoformat()
        stale_days = self.store.stale_trading_days(actual_market_as_of, expected_market_as_of)
        if actual_market_as_of != expected_market_as_of:
            return self._failed(
                selection_date, "local market snapshot is stale", pit_universe_count, reference,
                persist, metadata={
                    "market_as_of": actual_market_as_of,
                    "expected_market_as_of": expected_market_as_of,
                    "stale_trading_days": stale_days,
                    "decision_context": context.as_dict(),
                    "calendar_consensus": calendar,
                    **pit,
                },
            )
        history_counts = panel.groupby("symbol")["trade_date"].nunique()
        current_symbols = set(panel.loc[panel["trade_date"] == market_as_of, "symbol"])
        primary_symbols = set(panel.loc[
            (panel["trade_date"] == market_as_of) & (panel["provider"] == "zzshare"), "symbol"
        ])
        universe = universe.drop_duplicates("symbol").copy()
        universe["history_days"] = universe["symbol"].map(history_counts).fillna(0).astype(int)
        universe["has_current_bar"] = universe["symbol"].isin(current_symbols)
        coverage = float(universe["has_current_bar"].mean()) if len(universe) else 0.0
        primary_coverage = float(universe["symbol"].isin(primary_symbols).mean()) if len(universe) else 0.0
        if coverage < self.policy.min_warehouse_coverage:
            return self._failed(
                selection_date, "local warehouse coverage below threshold", pit_universe_count,
                reference, persist, coverage=coverage,
                metadata={"market_as_of": actual_market_as_of,
                          "expected_market_as_of": expected_market_as_of,
                          "stale_trading_days": stale_days,
                          "primary_coverage": primary_coverage,
                          "usable_qfq_coverage": coverage,
                          "decision_context": context.as_dict(),
                          "calendar_consensus": calendar,
                          **pit}
            )

        universe = self._hard_gates(universe, panel, market_as_of)
        if universe.empty:
            return self._failed(
                selection_date, "no securities passed local hard gates", pit_universe_count,
                reference, persist, coverage=coverage,
                metadata={**pit, "decision_context": context.as_dict()},
            )

        features = self._build_features(universe, panel, market_as_of)
        features = self._liquidity_gates(features)
        if features.empty:
            return self._failed(
                selection_date, "no securities passed liquidity gates", pit_universe_count,
                reference, persist, coverage=coverage,
                metadata={**pit, "decision_context": context.as_dict()},
            )
        breadth_frame = features.copy()
        latest_valuation_as_of = self.store.latest_valuation_as_of(actual_market_as_of)
        valuations = self.store.load_valuations(actual_market_as_of, exact=True)
        valuation_symbols = set(valuations.get("symbol", pd.Series(dtype=str)).astype(str))
        valuation_coverage = float(
            features["symbol"].isin(valuation_symbols).mean()
        ) if len(features) else 0.0
        valuation_stale_days = (
            self.store.stale_trading_days(latest_valuation_as_of, actual_market_as_of)
            if latest_valuation_as_of else None
        )
        if (latest_valuation_as_of != actual_market_as_of
                or valuation_coverage < self.policy.min_valuation_coverage):
            return self._failed(
                selection_date, "valuation snapshot stale or incomplete", pit_universe_count,
                reference, persist, coverage=coverage, metadata={
                    "market_as_of": actual_market_as_of,
                    "expected_market_as_of": expected_market_as_of,
                    "valuation_as_of": latest_valuation_as_of,
                    "valuation_coverage": round(valuation_coverage, 6),
                    "valuation_stale_trading_days": valuation_stale_days,
                    "decision_context": context.as_dict(),
                    "calendar_consensus": calendar,
                    **pit,
                },
            )
        financial_cutoff = context.financial_cutoff_at[:10]
        fundamentals = self._fundamentals(context)
        financial_revision_set_id = str(
            fundamentals.attrs.get("revision_set_id") or hashlib.sha256(b"").hexdigest()
        )
        frame = features
        if not valuations.empty and "symbol" in valuations.columns:
            frame = frame.merge(valuations, on="symbol", how="left", suffixes=("", "_valuation"))
        if not fundamentals.empty and "symbol" in fundamentals.columns:
            frame = frame.merge(fundamentals, on="symbol", how="left")
        frame = self._score_fundamentals(frame)
        qualified = frame["fundamental_metric_count"] >= self.policy.min_stock_fundamental_metrics
        financial_coverage = float(qualified.mean()) if len(frame) else 0.0
        if financial_coverage < self.policy.min_financial_universe_coverage:
            return self._failed(
                selection_date, "financial coverage below threshold", pit_universe_count, reference,
                persist, coverage=coverage, metadata={
                    "market_as_of": actual_market_as_of,
                    "expected_market_as_of": expected_market_as_of,
                    "stale_trading_days": stale_days,
                    "primary_coverage": primary_coverage,
                    "usable_qfq_coverage": coverage,
                    "valuation_as_of": latest_valuation_as_of,
                    "valuation_coverage": round(valuation_coverage, 6),
                    "valuation_stale_trading_days": valuation_stale_days,
                    "financial_coverage": financial_coverage,
                    "decision_context": context.as_dict(),
                    "calendar_consensus": calendar,
                    **pit,
                },
            )
        non_negative_equity = frame.get(
            "net_assets_positive", pd.Series(False, index=frame.index)
        ).fillna(False).astype(bool)
        frame = frame[qualified & non_negative_equity].copy()

        # Every local producer consumes the exact same fail-closed eligible PIT frame.
        eligible_scored = self._score_technical(frame)
        eligible_scored = self._score_industry_and_quality(eligible_scored, breadth_frame)
        eligible_scored = self._apply_events(eligible_scored, context)
        generation_after = (
            generation_reader(generation_capabilities) if callable(generation_reader) else {}
        )
        if generation_after != generation_before:
            if _generation_retry < 2:
                return self.run(
                    selection_date,
                    data_cutoff=data_cutoff,
                    decision_at=decision_at,
                    decision_mode=decision_mode,
                    wencai_reference=reference,
                    strategy_snapshot=strategy_snapshot,
                    persist=persist,
                    _generation_retry=_generation_retry + 1,
                )
            return self._failed(
                selection_date, "dataset_publication_unstable", pit_universe_count,
                reference, persist, coverage=coverage,
                metadata={
                    "decision_context": context.as_dict(),
                    "publication_generations_before": generation_before,
                    "publication_generations_after": generation_after,
                },
            )
        from analysis.local_reference_strategies import LocalReferenceStrategyEngine
        try:
            local_strategy_reference = LocalReferenceStrategyEngine(
                self.store, top_n=self.policy.nominations_per_local_strategy
            ).run(eligible_scored, market_as_of=actual_market_as_of)
        except Exception as exc:
            local_strategy_reference = {
                "rule_version": "local-satellite-v3",
                "market_as_of": actual_market_as_of,
                "reference_affects_score": False,
                "candidate_affects_membership": True,
                "strategies": {name: {
                    "status": "unavailable", "rows": [],
                    "reason": f"本地策略引擎异常:{type(exc).__name__}",
                } for name in ("主力资金", "低价擒牛", "低估值", "小市值", "净利增长")},
            }

        # Keep local-pit-v4 as the core producer.  Its internal percentile universe
        # is unchanged; local-fusion-v2 only composes its nominations afterwards.
        fundamental_pool = frame.sort_values(
            ["fundamental_score", "fundamental_coverage", "history_coverage"], ascending=False
        ).head(self.policy.fundamental_top_n).copy()

        fundamental_pool = self._score_technical(fundamental_pool)
        technical_pool = fundamental_pool.sort_values(
            ["technical_60_score", "fundamental_score"], ascending=False
        ).head(self.policy.technical_top_n).copy()
        technical_pool = self._score_industry_and_quality(technical_pool, breadth_frame)
        technical_pool = self._apply_events(technical_pool, context)
        technical_pool["total_score"] = (
            technical_pool["fundamental_score"] + technical_pool["technical_60_score"]
            + technical_pool["industry_score"]
            + technical_pool["data_quality_score"]
            + technical_pool["correction_120"] + technical_pool["correction_250"]
            + technical_pool["event_correction"]
        )
        technical_pool = technical_pool.sort_values(
            ["total_score", "data_coverage", "technical_60_score"], ascending=False
        )
        diversified = self._diversify(technical_pool)
        core_candidates = self._records(diversified.head(self.policy.diversified_top_n))
        if not core_candidates:
            return self._failed(
                selection_date, "local PIT core produced no eligible nominations",
                pit_universe_count, reference, persist, coverage=coverage,
                metadata={"market_as_of": actual_market_as_of,
                          "decision_context": context.as_dict(), **pit},
            )

        from analysis.local_fusion import (
            FusionPolicy, GenomeCandidateProducer, LocalFusionComposer, genome_snapshot,
        )
        recorded_pit_only = (
            strategy_snapshot.get("pit_only_mode")
            if isinstance(strategy_snapshot, dict) else None
        )
        pit_only_mode = (
            bool(recorded_pit_only) if recorded_pit_only is not None else
            os.getenv("LOCAL_FUSION_PIT_ONLY", "false").lower() in {
                "1", "true", "yes", "on"
            }
        )
        fusion_policy = FusionPolicy(
            top15_size=self.policy.final_n,
            top5_size=min(5, self.policy.final_n),
            top15_core_floor=self.policy.top15_core_floor,
            top15_satellite_cap=0 if pit_only_mode else self.policy.top15_satellite_cap,
            top15_timing_cap=0 if pit_only_mode else self.policy.top15_timing_cap,
            top5_core_floor=self.policy.top5_core_floor,
            top5_satellite_cap=0 if pit_only_mode else self.policy.top5_satellite_cap,
            top5_timing_cap=0 if pit_only_mode else self.policy.top5_timing_cap,
            nominations_per_local_strategy=self.policy.nominations_per_local_strategy,
            genome_nomination_cap=0 if pit_only_mode else self.policy.genome_nomination_cap,
            genome_prefilter_n=self.policy.genome_prefilter_n,
            genome_min_lane_score=self.policy.genome_min_lane_score,
            max_per_industry=self.policy.max_per_industry,
            max_pairwise_correlation=self.policy.max_pairwise_correlation,
            strategy_priority={
                "主力资金": self.policy.priority_main_force,
                "低价擒牛": self.policy.priority_low_price_bull,
                "低估值": self.policy.priority_value,
                "小市值": self.policy.priority_small_cap,
                "净利增长": self.policy.priority_profit_growth,
            },
        )
        locked_strategy_snapshot = dict(strategy_snapshot or genome_snapshot())
        locked_strategy_snapshot["pit_only_mode"] = pit_only_mode
        genome_result = (
            {"status": "paused", "rows": [], "strategy_snapshot": locked_strategy_snapshot,
             "reason": "LOCAL_FUSION_PIT_ONLY=true"}
            if pit_only_mode else GenomeCandidateProducer(fusion_policy).run(
                eligible_scored, panel, strategy_snapshot=locked_strategy_snapshot
            )
        )
        fusion = LocalFusionComposer(fusion_policy).compose(
            core_candidates, local_strategy_reference, genome_result, eligible_scored
        )
        candidates = fusion["top15"]
        formal_top5_candidates = fusion["top5"]
        comparison = self._comparison(candidates, reference)
        industry_values = frame["industry"].fillna("").astype(str).str.strip()
        industry_coverage = float(
            (industry_values.ne("") & industry_values.ne("未分类")).mean()
        ) if len(frame) else 0.0
        rule_version = fusion_policy.version
        market_dataset_ids = sorted({
            str(value) for value in panel.get("dataset_id", pd.Series(dtype=str)).dropna()
            if str(value).strip()
        })
        valuation_dataset_ids = sorted({
            str(value) for value in valuations.get("dataset_id", pd.Series(dtype=str)).dropna()
            if str(value).strip()
        })
        input_manifest = {
            "decision_context": context.as_dict(),
            "universe_snapshot_id": universe_snapshot_id,
            "market_dataset_ids": market_dataset_ids,
            "valuation_dataset_ids": valuation_dataset_ids,
            "financial_revision_set_id": financial_revision_set_id,
            "event_dataset_id": self.store.event_dataset_id(
                context.selection_date, cutoff_at=context.event_cutoff_at
            ),
            "policy_version": self.policy.version,
            "policy_hash": self.policy.policy_hash,
            "policy": self.policy.as_dict(),
            "fusion_policy": fusion_policy.as_dict(),
            "strategy_snapshot": locked_strategy_snapshot,
            "code_revision": context.code_revision,
            "dependency_lock_hash": dependency_lock_hash(),
            "publication_generations": generation_after,
        }
        manifest_id = hashlib.sha256(json.dumps(
            input_manifest, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
        input_manifest["manifest_id"] = manifest_id
        snapshot_payload = json.dumps({
            "selection_date": selection_date,
            "market_as_of": actual_market_as_of,
            "valuation_as_of": latest_valuation_as_of,
            "financial_as_of": pit.get("financial_pit_end_date"),
            "rule_version": rule_version,
            "policy_hash": self.policy.policy_hash,
            "decision_context": context.as_dict(),
            "manifest_id": manifest_id,
            "candidates": candidates,
            "formal_top5": formal_top5_candidates,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        snapshot_id = hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest()
        run = {
            "selection_date": selection_date,
            "status": "success" if candidates else "error",
            "universe_count": pit_universe_count,
            "eligible_count": int(len(frame)),
            "coverage": coverage,
            "comparison": comparison,
            "metadata": {
                "market_as_of": market_as_of.date().isoformat(),
                "expected_market_as_of": expected_market_as_of,
                "stale_trading_days": stale_days,
                "primary_coverage": round(primary_coverage, 6),
                "usable_qfq_coverage": round(coverage, 6),
                "valuation_as_of": latest_valuation_as_of,
                "valuation_coverage": round(valuation_coverage, 6),
                "valuation_stale_trading_days": valuation_stale_days,
                "financial_coverage": round(financial_coverage, 6),
                "financial_as_of": pit.get("financial_pit_end_date"),
                "data_cutoff": cutoff,
                "decision_context": context.as_dict(),
                "calendar_consensus": calendar,
                "snapshot_id": snapshot_id,
                "rule_version": rule_version,
                "policy_hash": self.policy.policy_hash,
                "policy": self.policy.as_dict(),
                "universe_snapshot_id": universe_snapshot_id,
                "manifest_id": manifest_id,
                **pit,
                "period_semantics": "trading_days",
                "primary_pipeline": "local_fusion",
                "core_pipeline": self.policy.core_rule_version,
                "reference_affects_score": False,
                "external_reference_affects_membership": False,
                "industry_coverage": round(industry_coverage, 6),
                "diversification_mode": (
                    "industry" if industry_coverage >= 0.95 else "industry_with_board_fallback"
                ),
                "max_pairwise_correlation": self.policy.max_pairwise_correlation,
                "correlation_matrix": self._last_correlation_matrix,
                "stage_counts": {
                    "fundamental": len(fundamental_pool), "technical": len(technical_pool),
                    "diversified": len(diversified), "core_nominations": len(core_candidates),
                    "local_nominations": sum(len(item.get("rows") or []) for item in
                                             local_strategy_reference["strategies"].values()),
                    "genome_nominations": len(genome_result.get("rows") or []),
                    "final": len(candidates),
                },
                "lane_counts": fusion["lane_counts"],
                "fusion_policy": fusion_policy.as_dict(),
                "strategy_snapshot_id": locked_strategy_snapshot.get("snapshot_id"),
                "pit_only_mode": pit_only_mode,
            },
            "input_manifest": input_manifest,
            "formal_top5_candidates": formal_top5_candidates,
        }
        if persist:
            run["run_id"] = self.store.save_selection(run, candidates, reference)
            formal_memberships = []
            for strategy_id, strategy_name, rows in (
                ("formal_local_fusion_top15", "正式本地融合TOP15", candidates),
                ("formal_local_fusion_top5", "正式本地融合TOP5", formal_top5_candidates),
            ):
                for rank, row in enumerate(rows, 1):
                    formal_memberships.append({
                        "symbol": row.get("symbol"), "lane": "formal",
                        "strategy_id": strategy_id, "strategy_name": strategy_name,
                        "strategy_version": fusion_policy.version, "lane_rank": rank,
                        "lane_score_raw": row.get("lane_score_raw", row.get("total_score", 0)),
                        "priority_weight": 1.0,
                        "evidence": {
                            "assigned_lane": row.get("assigned_lane"),
                            "primary_strategy": row.get("primary_strategy"),
                            "supporting_nominations": row.get("supporting_nominations") or [],
                        },
                    })
            eligible_symbols = sorted(eligible_scored["symbol"].astype(str).tolist())
            eligible_payload = {
                "snapshot_id": hashlib.sha256(json.dumps(
                    eligible_symbols, separators=(",", ":")
                ).encode("utf-8")).hexdigest(),
                "decision_context": context.as_dict(),
                "market_as_of": actual_market_as_of,
                "valuation_as_of": latest_valuation_as_of,
                "financial_as_of": pit.get("financial_pit_end_date"),
                "eligible_count": len(eligible_symbols),
                "excluded_count": max(0, pit_universe_count - len(eligible_symbols)),
                "symbols": eligible_symbols,
                "manifest_id": manifest_id,
            }
            for artifact_type, payload in (
                ("eligible_universe", eligible_payload),
                ("local_strategy_nominations", local_strategy_reference),
                ("genome_nominations", genome_result),
                ("candidate_nominations", fusion["nominations"]),
                ("formal_membership_nominations", formal_memberships),
                ("pit_only_top15", core_candidates[:self.policy.final_n]),
                ("fusion_policy", fusion_policy.as_dict()),
            ):
                self.store.save_selection_artifact(run["run_id"], artifact_type, payload)
            self.store.save_selection_strategy_records(
                run["run_id"], [*fusion["nominations"], *formal_memberships],
                policy=fusion_policy.as_dict(), policy_hash=fusion_policy.policy_hash,
                selection_date=selection_date,
                input_snapshot_id=eligible_payload["snapshot_id"],
                persist_policy=not pit_only_mode,
            )
        return {
            **run, "candidates": candidates, "wencai_reference": reference,
            "formal_top5": formal_top5_candidates,
            "core_candidates": core_candidates,
            "local_strategy_reference": local_strategy_reference,
            "genome_nominations": genome_result,
            "fusion": fusion,
        }

    def _failed(self, selection_date: str, reason: str, universe_count: int,
                reference: List[dict], persist: bool, *, coverage: float = 0.0,
                metadata: Optional[dict] = None) -> dict:
        run = {
            "selection_date": selection_date, "status": "incomplete",
            "universe_count": universe_count, "eligible_count": 0, "coverage": coverage,
            "comparison": self._comparison([], reference),
            "metadata": {"reason": reason, "primary_pipeline": "local_fusion",
                         "core_pipeline": self.policy.core_rule_version,
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
        result = result[
            listed
            & ~names.str.upper().str.contains(r"(?:^|\*)ST", regex=True)
            & ~names.str.contains("退", regex=False)
        ]
        if os.getenv("EXCLUDE_KCB", "true").lower() not in {"0", "false", "no", "off"}:
            result = result[~result["symbol"].str.startswith(("688", "689"))]
        minimum_history = max(
            self.policy.min_history_days, self.policy.min_listing_trading_days
        )
        result = result[result["history_days"] >= minimum_history]
        # 有效复权日线本身就是“已经交易了多少天”的直接证据。证券主表的
        # list_date 在部分供应商/历史快照中允许为空，而交易日历也可能只保留
        # 近期共识窗口；用两者反推上市天数会把拥有完整历史的老股误判为新股。
        # 前面的 max(min_history_days, min_listing_trading_days) 已确保此处只能
        # 放行具有足够真实交易历史的证券，list_date 继续作为展示元数据保留。
        result["listed_trading_days"] = result["history_days"]
        result = result[
            result["listed_trading_days"] >= self.policy.min_listing_trading_days
        ]
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

    def _fundamentals(self, context: DecisionContext) -> pd.DataFrame:
        selection_date = context.selection_date
        tables = {}
        for table in ("indicator", "income", "balance", "cash_flow"):
            frame = self.store.load_financial_history(
                table, selection_date, cutoff_at=context.financial_cutoff_at
            )
            if frame.empty:
                continue
            if "pub_date" in frame.columns:
                published = pd.to_datetime(frame["pub_date"], errors="coerce")
                frame = frame[published.notna() & (published <= pd.Timestamp(selection_date))]
            frame = frame.sort_values(["symbol", "stat_date", "pub_date"])
            tables[table] = frame
        symbols = sorted(set().union(*(
            set(frame["symbol"].astype(str)) for frame in tables.values()
        ))) if tables else []
        records = []
        revision_keys = []
        for symbol in symbols:
            periods = []
            for table in ("indicator", "income", "balance", "cash_flow"):
                frame = tables.get(table)
                if frame is None:
                    periods.append(set())
                else:
                    periods.append(set(
                        frame.loc[frame["symbol"].astype(str) == symbol, "stat_date"].dropna().astype(str)
                    ))
            common_periods = set.intersection(*periods) if periods and all(periods) else set()
            anchor = max(common_periods) if common_periods else None
            record = {
                "symbol": symbol, "anchor_stat_date": anchor,
                "statement_period_mismatch": not bool(anchor),
            }
            selected_periods = []
            for table, frame in tables.items():
                available = frame[frame["symbol"].astype(str) == symbol]
                if available.empty:
                    continue
                if anchor:
                    aligned = available[available["stat_date"].astype(str) == anchor]
                    row = aligned.iloc[-1]
                else:
                    row = available.sort_values(["stat_date", "pub_date"]).iloc[-1]
                selected_periods.append(str(row.get("stat_date") or ""))
                revision_keys.append(
                    f"{table}:{symbol}:{row.get('stat_date')}:{row.get('revision_no')}:{row.get('provider')}"
                )
                for key, value in row.items():
                    if key not in {"symbol", "as_of", "quality_status"}:
                        record[f"{table}_{key}"] = value
            # 单季/单年同比很容易被低基数或一次性损益放大。保留最新同比作为
            # “增长速度”，同时从已发布的最近三期计算正增长占比，作为“增长
            # 持续性”。缺少历史时保持未知，不用 0 静默填充。
            history_specs = {
                "net_profit_growth_positive_ratio": (
                    ("indicator", (
                        "net_profit_growth", "inc_net_profit_year_on_year",
                        "netprofit_yoy", "net_profit_yoy",
                    )),
                    ("income", (
                        "net_profit_growth", "np_parent_company_owners_yoy",
                        "netprofit_yoy", "n_income_attr_p_yoy",
                    )),
                ),
                "revenue_growth_positive_ratio": (
                    ("indicator", (
                        "revenue_growth", "inc_revenue_year_on_year",
                        "revenue_yoy", "or_yoy",
                    )),
                    ("income", (
                        "revenue_growth", "revenue_yoy", "operate_income_yoy",
                        "total_revenue_yoy",
                    )),
                ),
            }
            for metric_name, sources in history_specs.items():
                observations = pd.Series(dtype=float)
                for table, candidate_columns in sources:
                    frame = tables.get(table)
                    if frame is None:
                        continue
                    available = frame[frame["symbol"].astype(str) == symbol].copy()
                    if anchor:
                        available = available[
                            available["stat_date"].astype(str) <= str(anchor)
                        ]
                    column = next(
                        (key for key in candidate_columns if key in available.columns), None
                    )
                    if column:
                        observations = (
                            available.sort_values(["stat_date", "pub_date"])
                            .drop_duplicates("stat_date", keep="last")
                            .tail(3)[column]
                        )
                        observations = pd.to_numeric(observations, errors="coerce").dropna()
                        if not observations.empty:
                            break
                if not observations.empty:
                    record[f"history_{metric_name}"] = float(observations.gt(0).mean())
                    record[f"history_{metric_name}_periods"] = int(len(observations))
            if len(set(period for period in selected_periods if period)) > 1:
                record["statement_period_mismatch"] = True
            records.append(record)
        result = pd.DataFrame(records) if records else pd.DataFrame(columns=["symbol"])
        result.attrs["revision_set_id"] = hashlib.sha256(
            "\n".join(sorted(revision_keys)).encode("utf-8")
        ).hexdigest()
        return result

    def _build_features(self, universe: pd.DataFrame, panel: pd.DataFrame,
                        market_as_of: pd.Timestamp) -> pd.DataFrame:
        meta = universe.set_index("symbol")
        rows = []
        for symbol, bars in panel[panel["symbol"].isin(meta.index)].groupby("symbol"):
            bars = bars.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
            bars = bars.copy()
            bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
            bars = bars[bars["close"].notna() & (bars["close"] > 0)].copy()
            closes = bars["close"].to_numpy(dtype=float)
            if len(closes) < self.policy.min_history_days:
                continue
            volumes = pd.to_numeric(bars["volume"], errors="coerce").fillna(0).to_numpy(dtype=float)
            amounts = pd.to_numeric(bars.get("amount"), errors="coerce").fillna(0).to_numpy(dtype=float)
            return_series = bars.set_index("trade_date")["close"].pct_change().dropna().tail(60)
            rets = return_series.reset_index(drop=True)
            ma60 = float(np.mean(closes[-60:]))
            low60, high60 = float(np.min(closes[-60:])), float(np.max(closes[-60:]))
            industry = str(meta.loc[symbol].get("industry") or "未分类")
            limit_ratio = _price_limit_ratio(
                symbol, str(meta.loc[symbol].get("name") or ""), market_as_of
            )
            upper_limit = _limit_price(closes[-2], limit_ratio) if len(closes) >= 2 else None
            latest_high = _number(bars.iloc[-1].get("high"))
            latest_low = _number(bars.iloc[-1].get("low"))
            rows.append({
                "symbol": symbol, "name": meta.loc[symbol].get("name"), "industry": industry,
                "market": meta.loc[symbol].get("market"),
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
                "amount_5_vs_60": float(np.mean(amounts[-5:]) / np.mean(amounts[-60:]))
                    if len(amounts) >= 60 and np.mean(amounts[-60:]) > 0 else None,
                "amount_20_vs_60": float(np.mean(amounts[-20:]) / np.mean(amounts[-60:]))
                    if len(amounts) >= 60 and np.mean(amounts[-60:]) > 0 else None,
                "max_return_20": float(rets.tail(20).max()) if len(rets) >= 20 else None,
                "average_amount_20": float(np.mean(amounts[-20:])) if len(amounts) >= 20 else None,
                "average_amount_60": float(np.mean(amounts[-60:])) if len(amounts) >= 60 else None,
                "paused_days_20": int(pd.to_numeric(
                    bars.get("is_paused", pd.Series(0, index=bars.index)), errors="coerce"
                ).fillna(0).tail(20).astype(bool).sum()),
                "one_price_up": bool(
                    upper_limit is not None and latest_high is not None and latest_low is not None
                    and abs(latest_high - latest_low) < 0.0051
                    and abs(closes[-1] - upper_limit) < 0.0051
                ),
                "return_series_60": return_series,
                "correction_120": self._medium_correction(closes),
                "correction_250": self._long_correction(closes),
                "state": self._technical_state(closes),
                "history_coverage": min(1.0, len(closes) / self.policy.preferred_history_days),
            })
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame

        # 用每日横截面中位收益构造稳健市场代理，再剔除每只股票的滚动 Beta。
        # 这比“60 日涨幅减一个市场中位数”更接近可解释的个股残差趋势，也避免
        # 高 Beta 股票在普涨阶段仅凭系统性暴露获得高分。
        series_by_symbol = {
            str(row["symbol"]): row["return_series_60"]
            for row in rows if isinstance(row.get("return_series_60"), pd.Series)
        }
        return_matrix = (
            pd.concat(series_by_symbol, axis=1).sort_index().tail(60)
            if series_by_symbol else pd.DataFrame()
        )
        market_daily = return_matrix.median(axis=1, skipna=True)
        industry_by_symbol = frame.set_index("symbol")["industry"].astype(str).to_dict()
        industry_daily: Dict[str, pd.Series] = {}
        for industry in sorted(set(industry_by_symbol.values())):
            members = [
                symbol for symbol, label in industry_by_symbol.items()
                if label == industry and symbol in return_matrix.columns
            ]
            if industry and industry != "未分类" and len(members) >= 3:
                industry_daily[industry] = return_matrix[members].median(axis=1, skipna=True)

        beta_values: List[Optional[float]] = []
        market_residual_values: List[Optional[float]] = []
        industry_residual_values: List[Optional[float]] = []
        idio_vol_values: List[Optional[float]] = []
        for _, item in frame.iterrows():
            symbol = str(item["symbol"])
            stock_daily = return_matrix.get(symbol)
            aligned = pd.concat(
                [stock_daily, market_daily], axis=1, keys=["stock", "market"]
            ).dropna() if stock_daily is not None else pd.DataFrame()
            if len(aligned) >= 30 and float(aligned["market"].var()) > 0:
                beta = float(aligned["stock"].cov(aligned["market"]) / aligned["market"].var())
                residual = aligned["stock"] - beta * aligned["market"]
                market_residual = float((1.0 + residual.clip(lower=-0.99)).prod() - 1.0)
                idio_vol = float(residual.std() * math.sqrt(252))
            else:
                beta = None
                market_residual = None
                idio_vol = None
            industry_series = industry_daily.get(str(item.get("industry") or ""))
            industry_aligned = pd.concat(
                [stock_daily, industry_series], axis=1, keys=["stock", "industry"]
            ).dropna() if stock_daily is not None and industry_series is not None else pd.DataFrame()
            industry_residual = (
                float((1.0 + (industry_aligned["stock"] - industry_aligned["industry"])
                       .clip(lower=-0.99)).prod() - 1.0)
                if len(industry_aligned) >= 30 else None
            )
            beta_values.append(beta)
            market_residual_values.append(market_residual)
            industry_residual_values.append(industry_residual)
            idio_vol_values.append(idio_vol)

        frame["beta_60"] = beta_values
        frame["market_residual_momentum_60"] = market_residual_values
        frame["industry_residual_momentum_60"] = industry_residual_values
        frame["idiosyncratic_volatility_60"] = idio_vol_values

        market_return = frame["ret_60"].median(skipna=True)
        classified = (
            frame["industry"].fillna("").astype(str).str.strip().ne("")
            & frame["industry"].fillna("").astype(str).str.strip().ne("未分类")
        )
        industry_return = pd.Series(np.nan, index=frame.index, dtype=float)
        industry_return.loc[classified] = frame.loc[classified].groupby(
            "industry"
        )["ret_60"].transform("median")
        frame["market_excess_60"] = frame["market_residual_momentum_60"].where(
            frame["market_residual_momentum_60"].notna(), frame["ret_60"] - market_return
        )
        frame["industry_excess_60"] = frame["industry_residual_momentum_60"].where(
            frame["industry_residual_momentum_60"].notna(), frame["ret_60"] - industry_return
        )
        return frame

    def _liquidity_gates(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame
        average_amount = pd.to_numeric(frame["average_amount_20"], errors="coerce")
        paused = pd.to_numeric(frame["paused_days_20"], errors="coerce").fillna(0)
        one_price_up = frame["one_price_up"].fillna(False).astype(bool)
        return frame[
            (average_amount >= self.policy.min_average_amount_20)
            & (paused <= 2)
            & ~one_price_up
        ].copy()

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
            return pd.to_numeric(
                out.apply(lambda row: _first(row, names), axis=1), errors="coerce"
            ).astype(float)

        roe = values(("indicator_roe", "indicator_roe_weighted", "indicator_roe_ttm"))
        roa = values(("indicator_roa", "indicator_roa2", "indicator_roa_yearly"))
        gross_margin = values((
            "indicator_grossprofit_margin", "indicator_gross_profit_margin",
            "indicator_gross_margin", "income_gross_profit_margin",
        ))
        growth = values(("indicator_net_profit_growth", "indicator_inc_net_profit_year_on_year",
                         "income_net_profit_growth", "income_np_parent_company_owners_yoy"))
        revenue_growth = values((
            "indicator_revenue_growth", "indicator_inc_revenue_year_on_year",
            "indicator_or_yoy", "indicator_total_revenue_yoy",
            "income_revenue_growth", "income_revenue_yoy",
            "income_operating_revenue_yoy", "income_total_revenue_yoy",
        ))
        liabilities = values(("balance_total_liability", "balance_total_liabilities"))
        assets = values(("balance_total_assets", "balance_asset_total"))
        debt = liabilities / assets.replace(0, np.nan)
        ocf = values(("cash_flow_net_operate_cash_flow", "cash_flow_net_operating_cash_flow"))
        profit = values(("income_np_parent_company_owners", "income_net_profit"))
        cash_quality = ocf / profit.abs().replace(0, np.nan)
        mismatch = out.get(
            "statement_period_mismatch",
            pd.Series(True, index=out.index, dtype="boolean"),
        ).astype("boolean").fillna(True).astype(bool)
        cash_quality = cash_quality.where(~mismatch)
        pe = pd.to_numeric(out["pe_ttm"], errors="coerce") if "pe_ttm" in out else pd.Series(np.nan, index=out.index)
        pb = pd.to_numeric(out["pb"], errors="coerce") if "pb" in out else pd.Series(np.nan, index=out.index)
        pe = pe.where(pe > 0)
        pb = pb.where(pb > 0)
        dividend = pd.to_numeric(
            out.get("dividend_yield", pd.Series(np.nan, index=out.index)), errors="coerce"
        ).where(lambda values_: values_ >= 0)
        growth_stability = values(("history_net_profit_growth_positive_ratio",))
        revenue_stability = values(("history_revenue_growth_positive_ratio",))
        out["net_profit_growth_pct"] = growth
        out["revenue_growth_pct"] = revenue_growth
        out["net_profit_growth_positive_ratio"] = growth_stability
        out["revenue_growth_positive_ratio"] = revenue_stability
        out["debt_ratio"] = debt
        out["cash_quality"] = cash_quality
        out["roa"] = roa
        out["gross_margin"] = gross_margin
        out["net_profit_positive"] = pd.Series(pd.NA, index=out.index, dtype="boolean")
        out.loc[profit.notna(), "net_profit_positive"] = profit[profit.notna()].gt(0)
        industries = out.get("industry", pd.Series("", index=out.index))
        known_equity = assets.notna() & liabilities.notna()
        out["net_assets_positive"] = pd.Series(pd.NA, index=out.index, dtype="boolean")
        out.loc[known_equity, "net_assets_positive"] = (
            assets[known_equity] - liabilities[known_equity]
        ) > 0
        def family_mean(items: Sequence[Tuple[pd.Series, bool]]) -> pd.Series:
            numerator = pd.Series(0.0, index=out.index)
            denominator = pd.Series(0.0, index=out.index)
            for metric, higher_is_better in items:
                available = metric.notna().astype(float)
                ranked = _industry_percentile(
                    metric, industries, higher_is_better=higher_is_better
                )
                numerator += ranked * available
                denominator += available
            return (numerator / denominator.replace(0, np.nan)).fillna(0.0)

        profitability_quality = family_mean([
            (roe, True), (roa, True), (gross_margin, True),
        ])
        growth_quality = family_mean([
            (growth, True), (revenue_growth, True),
            (growth_stability, True), (revenue_stability, True),
        ])
        balance_quality = _industry_percentile(debt, industries, higher_is_better=False)
        cash_flow_quality = _industry_percentile(
            cash_quality.clip(-5, 5), industries, higher_is_better=True
        )
        valuation_quality = family_mean([
            (pe, False), (pb, False), (dividend, True),
        ])
        growth_divergence_penalty = (
            growth.gt(50) & revenue_growth.lt(0)
        ).astype(float) * 3.0
        out["fundamental_score"] = (
            profitability_quality * 10
            + growth_quality * 8
            + balance_quality * 6
            + cash_flow_quality * 6
            + valuation_quality * 10
            - growth_divergence_penalty
        ).clip(0.0, 40.0)
        out["growth_divergence_penalty"] = growth_divergence_penalty
        out["fundamental_coverage"] = pd.concat(
            [roe, roa, gross_margin, growth, revenue_growth, debt, cash_quality, pe, pb, dividend],
            axis=1,
        ).notna().mean(axis=1)
        out["fundamental_metric_count"] = pd.concat(
            [roe, roa, gross_margin, growth, revenue_growth, debt, cash_quality, pe, pb, dividend],
            axis=1,
        ).notna().sum(axis=1)
        return out

    @staticmethod
    def _score_technical(frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["technical_60_score"] = (
            _percentile(out["market_excess_60"]) * 7
            + _percentile(out["industry_excess_60"]) * 6
            + _percentile(out["ma60_slope"]) * 4
            + _percentile(out["persistence_60"]) * 3
            + _percentile(out["max_drawdown_60"]) * 3
            + _percentile(
                out["idiosyncratic_volatility_60"], higher_is_better=False
            ) * 3
            + _percentile(out["max_return_20"], higher_is_better=False) * 2
            + _percentile(out["amount_5_vs_60"]) * 1
            + _percentile(out["amount_20_vs_60"]) * 1
        )
        return out

    @staticmethod
    def _score_industry_and_quality(frame: pd.DataFrame, full_frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        breadth_frame = full_frame.assign(
            positive_return=full_frame["ret_60"] > 0,
            above_ma60_member=full_frame["above_ma60"] > 0,
            positive_ma60_slope=full_frame["ma60_slope"] > 0,
            near_60d_high=full_frame["range_pos_60"] >= 0.80,
            active_amount_proxy=full_frame["volume_20_vs_60"] >= 1.0,
        )
        grouped = breadth_frame.groupby("industry")
        breadth = grouped["positive_return"].mean()
        above_ma = grouped["above_ma60_member"].mean()
        slope_breadth = grouped["positive_ma60_slope"].mean()
        high_breadth = grouped["near_60d_high"].mean()
        participation = grouped["active_amount_proxy"].mean()
        sector_return = full_frame.groupby("industry")["ret_60"].median()
        out["industry_breadth"] = out["industry"].map(breadth)
        out["industry_above_ma60"] = out["industry"].map(above_ma)
        out["industry_positive_slope"] = out["industry"].map(slope_breadth)
        out["industry_near_high"] = out["industry"].map(high_breadth)
        out["industry_participation"] = out["industry"].map(participation)
        out["industry_return"] = out["industry"].map(sector_return)
        out["industry_score"] = (
            _percentile(out["industry_breadth"]) * 3
            + _percentile(out["industry_above_ma60"]) * 3
            + _percentile(out["industry_positive_slope"]) * 3
            + _percentile(out["industry_near_high"]) * 2
            + _percentile(out["industry_participation"]) * 2
            + _percentile(out["industry_return"]) * 2
        )
        out["industry_classified"] = (
            out["industry"].fillna("").astype(str).str.strip().ne("")
            & out["industry"].fillna("").astype(str).str.strip().ne("未分类")
        )
        # Missing industry data must not receive a synthetic median sector score.
        out.loc[~out["industry_classified"], "industry_score"] = 0.0
        valuation_columns = [c for c in ("pe_ttm", "pb") if c in out.columns]
        valuation_present = (out[valuation_columns].notna().mean(axis=1) if valuation_columns
                             else pd.Series(0.0, index=out.index))
        out["data_coverage"] = (
            out["history_coverage"] * 0.45 + out["fundamental_coverage"] * 0.40
            + valuation_present * 0.15
        ).clip(0, 1)
        out["data_quality_score"] = out["data_coverage"] * 15
        # Database compatibility only; formal payloads use data_quality_score.
        out["quality_score"] = out["data_quality_score"]
        return out

    def _apply_events(self, frame: pd.DataFrame, context: DecisionContext) -> pd.DataFrame:
        selection_date = context.selection_date
        out = frame.copy()
        events = self.store.load_events(
            selection_date, cutoff_at=context.event_cutoff_at
        )
        out["event_correction"] = 0.0
        if events.empty:
            return out
        event_types = {
            "监管处罚": "regulatory", "立案调查": "regulatory", "监管": "regulatory",
            "财务造假": "fraud", "业绩预告": "earnings", "业绩": "earnings",
            "重大合同": "contract", "合同": "contract", "股东增减持": "shareholder",
            "减持": "shareholder", "增持": "shareholder", "限售解禁": "lockup",
            "解禁": "lockup", "回购": "buyback", "诉讼仲裁": "litigation",
            "诉讼": "litigation", "股权质押": "pledge", "冻结": "pledge",
            "资产减值": "impairment", "商誉减值": "impairment",
            "审计意见": "audit",
        }
        events["canonical_type"] = events["event_type"].map(
            lambda value: event_types.get(str(value).strip(), str(value).strip().lower())
        )
        events["cluster_key"] = events["event_cluster_id"].fillna("").astype(str)
        missing_cluster = events["cluster_key"].eq("")
        events.loc[missing_cluster, "cluster_key"] = (
            events.loc[missing_cluster, "symbol"].astype(str) + ":"
            + events.loc[missing_cluster, "canonical_type"].astype(str) + ":"
            + events.loc[missing_cluster, "event_date"].astype(str)
        )
        events = events.sort_values(
            ["official", "materiality", "confidence"], ascending=False
        ).drop_duplicates(["symbol", "cluster_key"], keep="first")
        event_days = pd.to_datetime(events["effective_at"], errors="coerce")
        age = (pd.Timestamp(selection_date) - event_days).dt.days.clip(lower=0)
        half_life = events["canonical_type"].map({
            "regulatory": 60, "fraud": 120, "earnings": 30, "dividend": 20,
            "contract": 30, "shareholder": 45, "lockup": 20, "buyback": 30,
            "litigation": 60, "pledge": 60, "impairment": 90, "audit": 120,
        }).fillna(14)
        decay = np.power(0.5, age / half_life)
        novelty = pd.to_numeric(events["novelty"], errors="coerce").fillna(0).clip(0, 1)
        entity_weight = events["entity_impact"].map({
            "issuer": 1.0, "subsidiary": 0.7, "counterparty": 0.5,
        }).fillna(0.6)
        source_weight = np.where(
            events["source_family"].astype(str).eq("official_disclosure"), 1.0,
            np.where(events["official"].astype(bool), 1.0, 0.65),
        )
        raw = (pd.to_numeric(events["direction"], errors="coerce").fillna(0)
               * pd.to_numeric(events["confidence"], errors="coerce").fillna(0)
               * pd.to_numeric(events["materiality"], errors="coerce").fillna(0).clip(0, 1)
               * (1.0 + pd.to_numeric(events["surprise"], errors="coerce").fillna(0).clip(-1, 1) * 0.25)
               * (0.5 + novelty * 0.5) * entity_weight * decay * source_weight)
        # Bad news is intentionally asymmetric: risk correction dominates upside catalysts.
        events = events.assign(weighted=np.where(raw < 0, raw * 25, raw * 12))
        corrections = events.groupby("symbol")["weighted"].sum().clip(-25, 12)
        out["event_correction"] = out["symbol"].map(corrections).fillna(0.0)
        return out

    def _diversify(self, frame: pd.DataFrame) -> pd.DataFrame:
        rows, counts = [], {}
        matrix = []
        for _, row in frame.iterrows():
            industry = str(row.get("industry") or "").strip()
            bucket = (f"industry:{industry}" if industry and industry != "未分类"
                      else self._board_bucket(row))
            if counts.get(bucket, 0) >= self.policy.max_per_industry:
                continue
            rejected = False
            for selected in rows:
                result = self._correlation(row, selected)
                matrix.append({
                    "left": str(row.get("symbol") or ""),
                    "right": str(selected.get("symbol") or ""),
                    "status": result.status, "value": result.value,
                    "overlap_days": result.overlap_days,
                })
                if result.status != "known" or (
                    result.value is not None
                    and result.value >= self.policy.max_pairwise_correlation
                ):
                    rejected = True
                    break
            if rejected:
                continue
            rows.append(row)
            counts[bucket] = counts.get(bucket, 0) + 1
            if len(rows) >= self.policy.diversified_top_n:
                break
        self._last_correlation_matrix = matrix
        return pd.DataFrame(rows, columns=frame.columns)

    @staticmethod
    def _board_bucket(row: pd.Series) -> str:
        market = str(row.get("market") or "").strip()
        if market and market.lower() != "nan":
            return f"board:{market}"
        symbol = "".join(ch for ch in str(row.get("symbol") or "") if ch.isdigit())[-6:]
        if symbol.startswith(("300", "301")):
            board = "创业板"
        elif symbol.startswith(("4", "8", "92")):
            board = "北交所"
        elif symbol.startswith(("0", "2")):
            board = "深市主板"
        elif symbol.startswith(("688", "689")):
            board = "科创板"
        else:
            board = "沪市主板"
        return f"board:{board}"

    def _correlation(self, left: pd.Series, right: pd.Series) -> CorrelationResult:
        a = left.get("return_series_60")
        b = right.get("return_series_60")
        if not isinstance(a, pd.Series) or not isinstance(b, pd.Series):
            return CorrelationResult("insufficient", None, 0)
        aligned = pd.concat(
            [pd.to_numeric(a, errors="coerce"), pd.to_numeric(b, errors="coerce")],
            axis=1, join="inner",
        ).dropna()
        if len(aligned) < self.policy.min_correlation_days:
            return CorrelationResult("insufficient", None, len(aligned))
        corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
        if not math.isfinite(corr):
            return CorrelationResult("invalid", None, len(aligned))
        return CorrelationResult("known", corr, len(aligned))

    @staticmethod
    def _records(frame: pd.DataFrame) -> List[dict]:
        records = []
        for _, row in frame.iterrows():
            state = str(row.get("state") or "趋势观察")
            reasons = [
                f"60日市场残差趋势 {float(row.get('market_excess_60') or 0):+.1%}",
                f"60日行业残差趋势 {float(row.get('industry_excess_60') or 0):+.1%}",
                state,
            ]
            records.append({
                "symbol": row["symbol"], "name": row.get("name"),
                "industry": row.get("industry"), "state": state,
                "total_score": round(float(row["total_score"]), 4),
                "fundamental_score": round(float(row["fundamental_score"]), 4),
                "technical_60_score": round(float(row["technical_60_score"]), 4),
                "industry_score": round(float(row["industry_score"]), 4),
                "data_quality_score": round(float(row["data_quality_score"]), 4),
                "quality_score": round(float(row["data_quality_score"]), 4),
                "correction_120": round(float(row["correction_120"]), 4),
                "correction_250": round(float(row["correction_250"]), 4),
                "event_correction": round(float(row["event_correction"]), 4),
                "data_coverage": round(float(row["data_coverage"]), 4),
                "beta_60": _number(row.get("beta_60")),
                "idiosyncratic_volatility_60": _number(
                    row.get("idiosyncratic_volatility_60")
                ),
                "max_return_20": _number(row.get("max_return_20")),
                "score_components": {
                    "fundamental": round(float(row["fundamental_score"]), 4),
                    "technical_60": round(float(row["technical_60_score"]), 4),
                    "correction_120": round(float(row["correction_120"]), 4),
                    "correction_250": round(float(row["correction_250"]), 4),
                    "industry": round(float(row["industry_score"]), 4),
                    "data_quality": round(float(row["data_quality_score"]), 4),
                    "event": round(float(row["event_correction"]), 4),
                },
                "industry_breadth": {
                    "positive_return": _number(row.get("industry_breadth")),
                    "above_ma60": _number(row.get("industry_above_ma60")),
                    "positive_slope": _number(row.get("industry_positive_slope")),
                    "near_high": _number(row.get("industry_near_high")),
                    "participation": _number(row.get("industry_participation")),
                },
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
