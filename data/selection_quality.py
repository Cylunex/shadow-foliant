"""Shared classification and master freshness checks; no network or writes."""
from __future__ import annotations

import math
import pandas as pd


def classified_mask(labels: pd.Series) -> pd.Series:
    return ~labels.fillna("").astype(str).str.strip().str.lower().isin(
        {"", "未分类", "unknown", "nan", "none", "null", "<na>"}
    )


def input_quality(universe: pd.DataFrame, as_of: str, *,
                  min_industry_coverage: float = .90, max_master_age_days: int = 7) -> dict:
    labels = universe.get("industry", pd.Series("", index=universe.index))
    coverage = float(classified_mask(labels).mean()) if len(universe) else 0.0
    snapshot_date = universe.attrs.get("snapshot_date")
    try:
        age = (pd.Timestamp(as_of).date() - pd.Timestamp(snapshot_date).date()).days
    except (ValueError, TypeError, AttributeError):
        age = None
    industry_ready = coverage >= min_industry_coverage
    master_ready = (max_master_age_days <= 0 or
                    (age is not None and 0 <= age <= max_master_age_days))
    return {
        "industry_coverage": round(coverage, 6),
        "minimum_industry_coverage": min_industry_coverage,
        "industry_ready": industry_ready,
        "master_snapshot_date": snapshot_date, "master_age_days": age,
        "maximum_master_age_days": max_master_age_days, "master_ready": master_ready,
        "universe_scope": universe.attrs.get("universe_scope", "legacy"),
        "industry_source": universe.attrs.get("industry_classification", {}),
        "ready": bool(industry_ready and master_ready),
    }


def quality_warnings(metadata: dict) -> list[str]:
    warnings = []
    quality = metadata.get("input_quality") or {}
    coverage = quality.get("industry_coverage", metadata.get("industry_coverage"))
    try:
        if coverage is not None and (not math.isfinite(float(coverage)) or
                float(coverage) < float(quality.get("minimum_industry_coverage", .90))):
            warnings.append("行业覆盖不足，行业内比较及行业分散约束不完整")
    except (TypeError, ValueError):
        warnings.append("行业覆盖数据无效")
    if quality.get("master_ready") is False:
        warnings.append(f"证券主数据过期或日期不可核验：{quality.get('master_snapshot_date') or '未知'}")
    if metadata.get("valuation_status") == "lagged" or (
            metadata.get("data_degraded") and not warnings):
        warnings.append("选股输入存在降级，请核对数据日期与覆盖")
    return warnings
