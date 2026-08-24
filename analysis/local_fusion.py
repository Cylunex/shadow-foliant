"""Deterministic local multi-lane selection for ``local-fusion-v1``.

Only local PIT data can nominate formal candidates.  Wencai, Miaoxiang and LLM
reviews are deliberately absent from this module.  Cross-lane scores are not
added together: membership is decided by lane quotas and ranking by a fixed
policy order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from analysis.local_reference_strategies import STRATEGY_CONFIG


def _number(value: object) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _plain(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


@dataclass(frozen=True)
class FusionPolicy:
    version: str = "local-fusion-v1"
    top15_size: int = 15
    top5_size: int = 5
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
    max_per_industry: int = 5
    max_pairwise_correlation: float = 0.90
    strategy_priority: Mapping[str, float] = field(default_factory=lambda: {
        "主力资金": 1.0,
        "低价擒牛": 1.0,
        "低估值": 1.0,
        "小市值": 0.70,
        "净利增长": 0.50,
    })

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def policy_hash(self) -> str:
        payload = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def genome_snapshot(live_set: Optional[dict] = None) -> dict:
    if live_set is None:
        try:
            from analysis.strategy_genome import get_live_strategy_set
            live_set = get_live_strategy_set() or {}
        except Exception as exc:
            live_set = {"base": {}, "base_meta": {}, "composed": [],
                        "snapshot_error": type(exc).__name__}
    normalized = json.loads(json.dumps(live_set or {}, ensure_ascii=False, default=str))
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return {
        "snapshot_id": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "live_set": normalized,
    }


class GenomeCandidateProducer:
    """Scan a bounded local full-market prefilter with the deployed genome set."""

    _EXCLUDED_STRATEGIES = {"climax_limitdown"}

    def __init__(self, policy: FusionPolicy):
        self.policy = policy

    def run(self, eligible: pd.DataFrame, panel: pd.DataFrame, *,
            strategy_snapshot: Optional[dict] = None) -> dict:
        snapshot = strategy_snapshot or genome_snapshot()
        if eligible.empty or panel.empty:
            return {"status": "unavailable", "rows": [], "strategy_snapshot": snapshot,
                    "reason": "统一可投资快照或本地K线为空"}

        frame = eligible.copy()
        frame["genome_prefilter_score"] = (
            pd.to_numeric(frame.get("technical_60_score"), errors="coerce").fillna(0) * 0.50
            + pd.to_numeric(frame.get("fundamental_score"), errors="coerce").fillna(0) * 0.25
            + pd.to_numeric(frame.get("data_coverage"), errors="coerce").fillna(0) * 25
        )
        prefiltered = frame.sort_values(
            ["genome_prefilter_score", "symbol"], ascending=[False, True]
        ).head(self.policy.genome_prefilter_n)
        bars_by_symbol = {
            str(symbol): bars.sort_values("trade_date").copy()
            for symbol, bars in panel[panel["symbol"].isin(prefiltered["symbol"])].groupby("symbol")
        }
        try:
            from selection.instock_strategy_runner import STRATEGIES, run_one
        except Exception as exc:
            return {"status": "unavailable", "rows": [], "strategy_snapshot": snapshot,
                    "reason": f"基因组运行器不可用:{type(exc).__name__}"}
        strategy_ids = [sid for sid in STRATEGIES if sid not in self._EXCLUDED_STRATEGIES]
        rows: List[dict] = []
        live_set = snapshot.get("live_set") or {}
        for _, item in prefiltered.iterrows():
            symbol = str(item.get("symbol") or "")
            bars = bars_by_symbol.get(symbol)
            if bars is None or bars.empty:
                continue
            try:
                result = run_one(
                    symbol, bars, name=str(item.get("name") or ""),
                    strategies=strategy_ids, evolved=True, live_set=live_set,
                )
            except Exception:
                continue
            matched = list(result.get("matched") or [])
            if not matched:
                continue
            best_deploy = max(float(match.get("deployment_score") or 50) for match in matched)
            prefilter = _number(item.get("genome_prefilter_score")) or 0.0
            technical = _number(item.get("technical_60_score")) or 0.0
            fundamental = _number(item.get("fundamental_score")) or 0.0
            # One strong trigger matters more than a pile of correlated triggers.
            diversity_bonus = min(8.0, max(0, len({m.get("category") for m in matched}) - 1) * 2.0)
            lane_score = (
                best_deploy * 0.45
                + min(100.0, prefilter) * 0.20
                + min(100.0, technical / 30.0 * 100.0) * 0.20
                + min(100.0, fundamental / 40.0 * 100.0) * 0.15
                + diversity_bonus
            )
            if lane_score < self.policy.genome_min_lane_score:
                continue
            rows.append({
                "symbol": symbol,
                "name": _plain(item.get("name")),
                "industry": _plain(item.get("industry")),
                "lane_score": round(lane_score, 4),
                "data_coverage": _number(item.get("data_coverage")),
                "technical_state": _plain(item.get("state")),
                "matched": matched,
                "errors": list(result.get("errors") or []),
                "strategy_snapshot_id": snapshot.get("snapshot_id"),
            })
        rows.sort(key=lambda row: (-float(row["lane_score"]), str(row["symbol"])))
        rows = rows[:self.policy.genome_nomination_cap]
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
        return {
            "status": "ready" if rows else "empty",
            "rows": rows,
            "prefilter_count": int(len(prefiltered)),
            "strategy_snapshot": snapshot,
            "excluded_bearish_strategies": sorted(self._EXCLUDED_STRATEGIES),
        }


class LocalFusionComposer:
    def __init__(self, policy: Optional[FusionPolicy] = None):
        self.policy = policy or FusionPolicy()

    def compose(self, core_rows: Sequence[dict], local_result: dict,
                genome_result: dict, eligible: pd.DataFrame) -> dict:
        nominations = self._nominations(core_rows, local_result, genome_result)
        by_symbol: Dict[str, List[dict]] = {}
        for nomination in nominations:
            by_symbol.setdefault(nomination["symbol"], []).append(nomination)

        core = [item for item in nominations if item["lane"] == "core"]
        satellites = self._unique_lane(
            [item for item in nominations if item["lane"] == "satellite"], by_symbol,
            excluded={item["symbol"] for item in core[:self.policy.top15_size]},
        )
        timing = self._unique_lane(
            [item for item in nominations if item["lane"] == "timing"], by_symbol,
            excluded={item["symbol"] for item in core[:self.policy.top15_size]}
                     | {item["symbol"] for item in satellites},
        )
        feature_map = {
            str(row.get("symbol")): row for _, row in eligible.iterrows()
        }
        selected_by_lane: Dict[str, List[dict]] = {"core": [], "satellite": [], "timing": []}
        selected: List[dict] = []

        self._take(core, self.policy.top15_core_floor, "core", selected,
                   selected_by_lane, by_symbol, feature_map)
        self._take(satellites, self.policy.top15_satellite_cap, "satellite", selected,
                   selected_by_lane, by_symbol, feature_map)
        self._take(timing, self.policy.top15_timing_cap, "timing", selected,
                   selected_by_lane, by_symbol, feature_map)
        self._take(core, self.policy.top15_size, "core", selected,
                   selected_by_lane, by_symbol, feature_map)
        # A strict risk constraint can leave a slot empty; do not relax it merely
        # to make the output look complete.
        top15 = self._rank_with_template(
            selected_by_lane, ("core", "core", "satellite", "core", "timing",
                               "core", "satellite", "core", "core", "satellite",
                               "core", "timing", "core", "satellite", "satellite"),
            self.policy.top15_size,
        )

        top5_lanes = {
            "core": selected_by_lane["core"][:self.policy.top5_core_floor],
            "satellite": selected_by_lane["satellite"][:self.policy.top5_satellite_cap],
            "timing": selected_by_lane["timing"][:self.policy.top5_timing_cap],
        }
        top5 = self._rank_with_template(
            top5_lanes, ("core", "core", "satellite", "core", "timing"),
            self.policy.top5_size, fallback=top15,
        )
        return {
            "policy": self.policy.as_dict(),
            "policy_hash": self.policy.policy_hash,
            "nominations": nominations,
            "top15": top15,
            "top5": top5,
            "lane_counts": {lane: len(rows) for lane, rows in selected_by_lane.items()},
        }

    def _nominations(self, core_rows: Sequence[dict], local_result: dict,
                     genome_result: dict) -> List[dict]:
        nominations: List[dict] = []
        for rank, row in enumerate(core_rows, 1):
            nominations.append({
                "symbol": str(row.get("symbol") or ""), "lane": "core",
                "strategy_id": "local_pit_v4", "strategy_name": "本地PIT",
                "strategy_version": "local-pit-v4", "strategy_family": "core",
                "lane_rank": rank, "lane_score_raw": _number(row.get("total_score")) or 0.0,
                "priority_weight": 1.0, "evidence": dict(row),
            })
        for strategy_name, result in (local_result.get("strategies") or {}).items():
            config = dict(STRATEGY_CONFIG.get(strategy_name) or {})
            priority = float(self.policy.strategy_priority.get(
                strategy_name, config.get("priority_weight", 0.5)
            ))
            if result.get("status") not in {"ready", "degraded"}:
                continue
            for rank, row in enumerate(
                list(result.get("rows") or [])[:self.policy.nominations_per_local_strategy], 1
            ):
                nominations.append({
                    "symbol": str(row.get("symbol") or ""), "lane": "satellite",
                    "strategy_id": config.get("strategy_id") or strategy_name,
                    "strategy_name": strategy_name,
                    "strategy_version": local_result.get("rule_version") or "local-satellite-v2",
                    "strategy_family": config.get("family") or "satellite",
                    "lane_rank": rank, "lane_score_raw": _number(row.get("lane_score")) or 0.0,
                    "priority_weight": priority, "evidence": dict(row),
                })
        for rank, row in enumerate(genome_result.get("rows") or [], 1):
            nominations.append({
                "symbol": str(row.get("symbol") or ""), "lane": "timing",
                "strategy_id": "technical_timing_genome",
                "strategy_name": "技术基因组",
                "strategy_version": str(row.get("strategy_snapshot_id") or "unknown"),
                "strategy_family": "technical_timing", "lane_rank": rank,
                "lane_score_raw": _number(row.get("lane_score")) or 0.0,
                "priority_weight": 1.0, "evidence": dict(row),
            })
        return [item for item in nominations if item["symbol"]]

    @staticmethod
    def _unique_lane(rows: Sequence[dict], all_by_symbol: Mapping[str, Sequence[dict]],
                     excluded: set[str]) -> List[dict]:
        best: Dict[str, dict] = {}
        for row in rows:
            symbol = row["symbol"]
            if symbol in excluded:
                continue
            current = best.get(symbol)
            key = (float(row.get("priority_weight") or 0),
                   float(row.get("lane_score_raw") or 0), -int(row.get("lane_rank") or 999))
            if current is None or key > (
                float(current.get("priority_weight") or 0),
                float(current.get("lane_score_raw") or 0),
                -int(current.get("lane_rank") or 999),
            ):
                best[symbol] = row
        return sorted(best.values(), key=lambda row: (
            -float(row.get("priority_weight") or 0),
            -float(row.get("lane_score_raw") or 0), int(row.get("lane_rank") or 999),
            str(row["symbol"]),
        ))

    def _take(self, pool: Sequence[dict], limit: int, lane: str, selected: List[dict],
              selected_by_lane: Dict[str, List[dict]], all_by_symbol: Mapping[str, Sequence[dict]],
              feature_map: Mapping[str, pd.Series]) -> None:
        for nomination in pool:
            if len(selected_by_lane[lane]) >= limit or len(selected) >= self.policy.top15_size:
                break
            if any(item["symbol"] == nomination["symbol"] for item in selected):
                continue
            if not self._passes_diversification(nomination["symbol"], selected, feature_map):
                continue
            candidate = self._candidate(nomination, all_by_symbol.get(nomination["symbol"], []))
            feature = feature_map.get(nomination["symbol"])
            if feature is not None:
                candidate["name"] = candidate.get("name") or _plain(feature.get("name"))
                candidate["industry"] = candidate.get("industry") or _plain(feature.get("industry"))
                candidate["data_coverage"] = (
                    candidate.get("data_coverage")
                    if candidate.get("data_coverage") is not None
                    else _number(feature.get("data_coverage"))
                )
                candidate["state"] = candidate.get("state") or _plain(feature.get("state"))
            selected.append(candidate)
            selected_by_lane[lane].append(candidate)

    def _passes_diversification(self, symbol: str, selected: Sequence[dict],
                                features: Mapping[str, pd.Series]) -> bool:
        row = features.get(symbol)
        if row is None:
            return False
        industry = str(row.get("industry") or "未分类")
        if industry and industry != "未分类":
            same = sum(1 for item in selected if str(item.get("industry") or "") == industry)
            if same >= self.policy.max_per_industry:
                return False
        returns = row.get("return_series_60")
        if not isinstance(returns, pd.Series):
            return True
        for item in selected:
            other = features.get(item["symbol"])
            other_returns = other.get("return_series_60") if other is not None else None
            if not isinstance(other_returns, pd.Series):
                continue
            aligned = pd.concat([returns, other_returns], axis=1, join="inner").dropna()
            if len(aligned) < 40:
                continue
            corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
            if pd.notna(corr) and float(corr) > self.policy.max_pairwise_correlation:
                return False
        return True

    @staticmethod
    def _candidate(primary: dict, nominations: Sequence[dict]) -> dict:
        evidence = dict(primary.get("evidence") or {})
        supporting = [{
            "lane": item.get("lane"), "strategy_id": item.get("strategy_id"),
            "strategy_name": item.get("strategy_name"),
            "strategy_version": item.get("strategy_version"),
            "lane_rank": item.get("lane_rank"),
            "lane_score_raw": item.get("lane_score_raw"),
        } for item in sorted(nominations, key=lambda item: (
            str(item.get("lane")), int(item.get("lane_rank") or 999)
        ))]
        labels = [str(item.get("strategy_name")) for item in supporting]
        return {
            "symbol": primary["symbol"], "name": evidence.get("name"),
            "industry": evidence.get("industry"), "assigned_lane": primary["lane"],
            "primary_strategy": primary["strategy_id"],
            "primary_strategy_name": primary["strategy_name"],
            "lane_rank": primary["lane_rank"],
            "lane_score_raw": round(float(primary["lane_score_raw"]), 4),
            "strategy_priority_weight": float(primary.get("priority_weight") or 0),
            "supporting_nominations": supporting, "source_labels": labels,
            "total_score": round(float(primary["lane_score_raw"]), 4),
            "local_score": round(float(primary["lane_score_raw"]), 4),
            "fundamental_score": _number(evidence.get("fundamental_score")),
            "technical_60_score": _number(evidence.get("technical_60_score")),
            "industry_score": _number(evidence.get("industry_score")),
            "quality_score": _number(evidence.get("quality_score")),
            "data_quality_score": _number(evidence.get("data_quality_score")),
            "correction_120": _number(evidence.get("correction_120")),
            "correction_250": _number(evidence.get("correction_250")),
            "event_correction": _number(evidence.get("event_correction")),
            "data_coverage": _number(evidence.get("data_coverage")),
            "state": evidence.get("state") or evidence.get("technical_state"),
            "score_components": dict(evidence.get("score_components") or {}),
            "reasons": [f"{primary['strategy_name']}赛道第{primary['lane_rank']}名"],
        }

    @staticmethod
    def _rank_with_template(lanes: Dict[str, List[dict]], template: Sequence[str],
                            limit: int, fallback: Optional[Sequence[dict]] = None) -> List[dict]:
        queues = {lane: list(rows) for lane, rows in lanes.items()}
        ranked: List[dict] = []
        used: set[str] = set()
        for lane in template:
            queue = queues.get(lane) or []
            while queue and queue[0]["symbol"] in used:
                queue.pop(0)
            if queue:
                item = queue.pop(0)
                ranked.append(dict(item))
                used.add(item["symbol"])
        remainder = [item for rows in queues.values() for item in rows]
        remainder.extend(list(fallback or []))
        for item in remainder:
            if len(ranked) >= limit:
                break
            if item["symbol"] not in used:
                ranked.append(dict(item))
                used.add(item["symbol"])
        for rank, item in enumerate(ranked[:limit], 1):
            item["selection_priority"] = rank
            item["rank"] = rank
        return ranked[:limit]
