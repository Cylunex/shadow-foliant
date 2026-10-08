"""可降级的市场宽度快照。

使用当日校验的中证 A500 成分列表，再批量取一次实时报价。固定报告节点
在盘前预取缺失时可有界自愈；失败仍明确关闭买入侧。
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, Optional


def evaluate_quotes(quotes: Dict[str, dict], expected: Optional[int] = None) -> Dict[str, Any]:
    changes = []
    for quote in (quotes or {}).values():
        try:
            value = float((quote or {}).get("change_pct"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            changes.append(value)
    covered = len(changes)
    expected = int(expected or covered)
    coverage = covered / expected if expected > 0 else 0.0
    if covered < 100 or coverage < 0.5:
        return {
            "available": False, "covered": covered, "expected": expected,
            "coverage": round(coverage, 3), "reason": "有效横截面覆盖不足",
            "failure_code": "a500_breadth_coverage_insufficient",
        }
    up = sum(1 for x in changes if x > 0)
    down = sum(1 for x in changes if x < 0)
    flat = covered - up - down
    return {
        "available": True,
        "universe": "中证A500",
        "covered": covered,
        "expected": expected,
        "coverage": round(coverage, 3),
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "up_ratio": round(up / covered, 4),
        "down_ratio": round(down / covered, 4),
        "median_change": round(statistics.median(changes), 3),
        "limit_up_like": sum(1 for x in changes if x >= 9.5),
        "limit_down_like": sum(1 for x in changes if x <= -9.5),
    }


def build(symbols: Optional[Iterable[str]] = None, force: bool = False) -> Dict[str, Any]:
    cache_get = cache_set = None
    cache_key = "market_breadth:a500:realtime"
    if symbols is None:
        try:
            from cache import cache_get, cache_set
            if not force:
                cached = cache_get(cache_key)
                if isinstance(cached, dict) and cached.get("available"):
                    return cached
        except Exception:
            cache_get = cache_set = None
    codes = [str(x).zfill(6) for x in (symbols or []) if x]
    provenance = {}
    if symbols is None:
        from analysis.a500_universe import current, refresh
        universe = current()
        if not universe.get("available") and force:
            universe = refresh()
        if not universe.get("available"):
            return {
                "available": False, "covered": 0,
                "reason": "当日校验的 A500 成分未就绪",
                "failure_code": universe.get("failure_code") or "a500_constituents_missing",
                "constituents_source_date": universe.get("source_date"),
                "constituents_error_type": universe.get("error_type"),
            }
        codes = universe["codes"]
        provenance = {
            "constituents_source": universe["source"],
            "constituents_source_date": universe["source_date"],
            "constituents_fetched_at": universe["fetched_at"],
        }
    codes = list(dict.fromkeys(str(x).zfill(6) for x in codes if x))
    if not codes:
        return {
            "available": False, "covered": 0, "reason": "A500 成分缓存未就绪",
            "failure_code": "a500_constituents_missing",
        }
    try:
        import datahub
        result = evaluate_quotes(datahub.quotes(codes), expected=len(codes))
        result.update(provenance)
        if cache_set and result.get("available"):
            cache_set(cache_key, result, 60)
        return result
    except Exception as exc:
        return {"available": False, "covered": 0,
                "reason": f"{type(exc).__name__}: {str(exc)[:80]}",
                "failure_code": "a500_quotes_failed"}
