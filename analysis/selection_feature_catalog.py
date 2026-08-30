"""Versioned feature contract shared by formal selection and research reporting.

The catalog owns semantics and fixed production weights.  It deliberately contains no
optimizer: evidence may propose a later catalog version, but a research run cannot mutate
the live scoring policy in place.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Dict, Mapping, Tuple

import numpy as np
import pandas as pd


CATALOG_VERSION = "selection-features-v2"


@dataclass(frozen=True)
class SelectionFeature:
    key: str
    family: str
    direction: int
    weight: float
    lookback_days: int
    missing_policy: str = "zero_component"
    description: str = ""

    def public_dict(self) -> dict:
        return asdict(self)


TECHNICAL_FEATURES: Tuple[SelectionFeature, ...] = (
    SelectionFeature("market_excess_60", "residual_momentum", +1, 7, 60,
                     description="60-day return after market common movement"),
    SelectionFeature("industry_excess_60", "residual_momentum", +1, 6, 60,
                     description="60-day return after industry common movement"),
    SelectionFeature("ma60_slope", "trend", +1, 4, 60),
    SelectionFeature("persistence_60", "trend", +1, 3, 60),
    SelectionFeature("max_drawdown_60", "risk", +1, 3, 60),
    SelectionFeature("idiosyncratic_volatility_60", "risk", -1, 3, 60),
    SelectionFeature("max_return_20", "lottery", -1, 2, 20),
    SelectionFeature("amount_5_vs_60", "liquidity", +1, 1, 60),
    SelectionFeature("amount_20_vs_60", "liquidity", +1, 1, 60),
)

FUNDAMENTAL_FAMILY_WEIGHTS: Dict[str, float] = {
    "profitability_quality": 10,
    "growth_quality": 8,
    "balance_quality": 6,
    "cash_flow_quality": 6,
    "valuation_quality": 10,
}

INDUSTRY_FEATURE_WEIGHTS: Dict[str, float] = {
    "industry_breadth": 3,
    "industry_above_ma60": 3,
    "industry_positive_slope": 3,
    "industry_near_high": 2,
    "industry_participation": 2,
    "industry_return": 2,
}

DATA_QUALITY_WEIGHT = 15.0


def catalog_manifest() -> dict:
    return {
        "version": CATALOG_VERSION,
        "technical": [item.public_dict() for item in TECHNICAL_FEATURES],
        "fundamental_family_weights": dict(FUNDAMENTAL_FAMILY_WEIGHTS),
        "industry_feature_weights": dict(INDUSTRY_FEATURE_WEIGHTS),
        "data_quality_weight": DATA_QUALITY_WEIGHT,
        "mutable_by_research": False,
    }


def _numeric_column(frame: pd.DataFrame, *names: str) -> pd.Series:
    for name in names:
        if name in frame.columns:
            return pd.to_numeric(frame[name], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype=float)


def compute_stock_feature_series(frame: pd.DataFrame) -> Dict[str, pd.Series]:
    """Compute the production price/amount features for every observation.

    This is the only implementation used by both the formal selector and factor
    research.  Callers may evaluate older exploratory ``factor_zoo`` features,
    but production keys must never be reimplemented there under a similar name.
    """
    close = _numeric_column(frame, "close", "Close", "收盘")
    amount = _numeric_column(frame, "amount", "turnover", "成交额")
    daily_return = close.pct_change()
    ma60 = close.rolling(60, min_periods=60).mean()

    def rolling_drawdown(values: np.ndarray) -> float:
        values = np.asarray(values, dtype=float)
        if len(values) < 60 or not np.isfinite(values).all():
            return np.nan
        peaks = np.maximum.accumulate(values)
        return float(np.min(values / peaks - 1.0))

    return {
        # Internal input for market/industry residual features.  It is returned
        # deliberately but is not part of the weighted public catalog.
        "ret_60": close / close.shift(60) - 1.0,
        "ma60_slope": ma60 / ma60.shift(10) - 1.0,
        "persistence_60": daily_return.gt(0).rolling(60, min_periods=60).mean(),
        "max_drawdown_60": close.rolling(60, min_periods=60).apply(
            rolling_drawdown, raw=True
        ),
        "max_return_20": daily_return.rolling(20, min_periods=20).max(),
        "amount_5_vs_60": (
            amount.rolling(5, min_periods=5).mean()
            / amount.rolling(60, min_periods=60).mean().replace(0, np.nan)
        ),
        "amount_20_vs_60": (
            amount.rolling(20, min_periods=20).mean()
            / amount.rolling(60, min_periods=60).mean().replace(0, np.nan)
        ),
    }


def compute_cross_sectional_snapshot(
    base_features: pd.DataFrame,
    return_matrix: pd.DataFrame,
    industry_by_symbol: Mapping[str, str],
) -> pd.DataFrame:
    """Complete one production feature cross-section without future data.

    ``return_matrix`` must contain only observations visible at the snapshot and
    is truncated to the latest 60 rows here as a second guard.  Industry
    residuals stay unavailable for unclassified or tiny (fewer than three)
    cohorts instead of silently substituting market exposure.
    """
    result = base_features.copy()
    if result.empty:
        return result
    returns = return_matrix.copy().sort_index().tail(60)
    returns.columns = returns.columns.astype(str)
    market_daily = returns.median(axis=1, skipna=True)
    industry_daily: Dict[str, pd.Series] = {}
    labels = {str(symbol): str(label or "") for symbol, label in industry_by_symbol.items()}
    for industry in sorted(set(labels.values())):
        members = [symbol for symbol, label in labels.items()
                   if label == industry and symbol in returns.columns]
        if industry and industry != "未分类" and len(members) >= 3:
            industry_daily[industry] = returns[members].median(axis=1, skipna=True)

    betas = []
    market_residuals = []
    industry_residuals = []
    idio_vols = []
    for symbol in result.index.astype(str):
        stock_daily = returns.get(symbol)
        aligned = (pd.concat([stock_daily, market_daily], axis=1,
                             keys=["stock", "market"]).dropna()
                   if stock_daily is not None else pd.DataFrame())
        if len(aligned) >= 30 and float(aligned["market"].var()) > 0:
            beta = float(aligned["stock"].cov(aligned["market"])
                         / aligned["market"].var())
            residual = aligned["stock"] - beta * aligned["market"]
            market_residual = float(
                (1.0 + residual.clip(lower=-0.99)).prod() - 1.0
            )
            idio_vol = float(residual.std() * math.sqrt(252))
        else:
            beta = market_residual = idio_vol = np.nan

        industry_series = industry_daily.get(labels.get(symbol, ""))
        industry_aligned = (pd.concat([stock_daily, industry_series], axis=1,
                                      keys=["stock", "industry"]).dropna()
                            if stock_daily is not None and industry_series is not None
                            else pd.DataFrame())
        industry_residual = (
            float((1.0 + (industry_aligned["stock"] - industry_aligned["industry"])
                   .clip(lower=-0.99)).prod() - 1.0)
            if len(industry_aligned) >= 30 else np.nan
        )
        betas.append(beta)
        market_residuals.append(market_residual)
        industry_residuals.append(industry_residual)
        idio_vols.append(idio_vol)

    result["beta_60"] = betas
    result["market_residual_momentum_60"] = market_residuals
    result["industry_residual_momentum_60"] = industry_residuals
    result["idiosyncratic_volatility_60"] = idio_vols
    market_return = pd.to_numeric(result.get("ret_60"), errors="coerce").median()
    industry_return = pd.Series(np.nan, index=result.index, dtype=float)
    industry_labels = pd.Series(
        [labels.get(str(symbol), "") for symbol in result.index], index=result.index
    )
    classified = industry_labels.ne("") & industry_labels.ne("未分类")
    if classified.any():
        industry_return.loc[classified] = pd.to_numeric(
            result.loc[classified, "ret_60"], errors="coerce"
        ).groupby(industry_labels.loc[classified]).transform("median")
    result["market_excess_60"] = result["market_residual_momentum_60"].where(
        result["market_residual_momentum_60"].notna(),
        pd.to_numeric(result.get("ret_60"), errors="coerce") - market_return,
    )
    result["industry_excess_60"] = result["industry_residual_momentum_60"].where(
        result["industry_residual_momentum_60"].notna(),
        pd.to_numeric(result.get("ret_60"), errors="coerce") - industry_return,
    )
    return result
