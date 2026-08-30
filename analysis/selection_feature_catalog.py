"""Versioned feature contract shared by formal selection and research reporting.

The catalog owns semantics and fixed production weights.  It deliberately contains no
optimizer: evidence may propose a later catalog version, but a research run cannot mutate
the live scoring policy in place.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Tuple


CATALOG_VERSION = "selection-features-v1"


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

