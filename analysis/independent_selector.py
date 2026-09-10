"""A reproducible selection lane independent from formal and Wencai membership.

The lane reads only the immutable PIT inputs named by an already-published
selection manifest.  It deliberately never accepts formal/Wencai rows as an
input, so those lists cannot influence eligibility, scores, rank or ties.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Iterable

import numpy as np
import pandas as pd

from analysis.local_stock_selector import LocalStockSelector, SelectionPolicy, _percentile
from core.decision_context import DecisionContext
from data.research_store import ResearchStore


STRATEGY_ID = "codex-independent"
STRATEGY_VERSION = "codex-independent-v1"
ARTIFACT_TYPE = "independent_selection"
REPAIR_ARTIFACT_TYPE = "independent_selection_repair"
EXPECTED_WENCAI_STRATEGIES = {
    "主力资金", "低价擒牛", "小市值", "净利增长", "低估值",
}


@dataclass(frozen=True)
class IndependentPolicy:
    version: str = STRATEGY_VERSION
    fundamental_quality: int = 30
    medium_trend: int = 25
    valuation: int = 20
    flow_liquidity: int = 15
    risk_discount: int = 10
    minimum_history_days: int = 70
    minimum_average_amount_20: float = 20_000_000.0
    required_fields: tuple[str, ...] = (
        "roe", "net_profit_growth_pct", "debt_ratio", "cash_quality",
        "ret_60", "ma60_slope", "persistence_60", "pe_ttm", "pb",
        "average_amount_20", "volume_20_vs_60", "amount_20_vs_60",
        "max_drawdown_60", "volatility_60",
    )

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["required_fields"] = list(self.required_fields)
        value["component_weights"] = {
            "fundamental_quality": self.fundamental_quality,
            "medium_trend": self.medium_trend,
            "valuation": self.valuation,
            "flow_liquidity": self.flow_liquidity,
            "risk_discount": self.risk_discount,
        }
        value["subweights"] = {
            "fundamental_quality": {"roe": 8, "net_profit_growth_pct": 8,
                                    "debt_ratio": 7, "cash_quality": 7},
            "medium_trend": {"ret_60": 9, "ma60_slope": 8, "persistence_60": 8},
            "valuation": {"pe_ttm": 10, "pb": 10},
            "flow_liquidity": {"average_amount_20": 5, "volume_20_vs_60": 5,
                               "amount_20_vs_60": 5},
            "risk_discount": {"max_drawdown_60": 5, "volatility_60": 5},
        }
        return value

    @property
    def policy_hash(self) -> str:
        return _hash(self.public_dict())


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _frame_hash(frame: pd.DataFrame, columns: Iterable[str]) -> str:
    selected = [column for column in columns if column in frame.columns]
    if not selected:
        return hashlib.sha256(b"").hexdigest()
    normalized = frame[selected].copy().sort_values(selected[:2]).reset_index(drop=True)
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def artifact_payload(artifacts: dict[str, Any]) -> dict[str, Any]:
    """Prefer an append-only ready repair while retaining the failed first attempt."""
    base = ((artifacts.get(ARTIFACT_TYPE) or {}).get("payload") or {})
    repair = ((artifacts.get(REPAIR_ARTIFACT_TYPE) or {}).get("payload") or {})
    if repair.get("status") == "ready":
        return repair
    return base or repair or {
        "status": "unavailable", "reason": "artifact_missing", "top15": [], "top5": [],
    }


def _symbol(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


class _FrozenManifestStore:
    """Minimal store surface consumed by LocalStockSelector feature helpers."""

    def __init__(self, *, financials: dict[str, pd.DataFrame], events: pd.DataFrame):
        self.financials = financials
        self.events = events

    def load_financial_history(self, table, _as_of, *, cutoff_at=None):
        return self.financials.get(str(table), pd.DataFrame()).copy()

    def load_events(self, _as_of, *, lookback_days=120, cutoff_at=None):
        return self.events.copy()


def _context(value: dict[str, Any]) -> DecisionContext:
    fields = DecisionContext.__dataclass_fields__
    return DecisionContext(**{name: value[name] for name in fields})


def _prepare_frame(store: ResearchStore, manifest_id: str,
                   policy: IndependentPolicy) -> tuple[pd.DataFrame, dict[str, Any]]:
    manifest = store.load_selection_manifest(manifest_id)
    if not manifest:
        return pd.DataFrame(), {"reason": "manifest_missing"}
    universe = store.load_universe_from_manifest(manifest_id)
    panel = store.load_daily_panel_from_manifest(manifest_id)
    valuations = store.load_valuations_from_manifest(manifest_id)
    financials = store.load_financial_facts_from_manifest(manifest_id)
    try:
        events = store.load_events_from_manifest(manifest_id)
    except ValueError:
        return pd.DataFrame(), {"reason": "event_revision_set_mismatch"}
    missing_inputs = [name for name, value in (
        ("universe", universe), ("daily_panel", panel), ("valuation", valuations),
    ) if value is None or value.empty]
    missing_inputs.extend(
        f"financial:{name}" for name in ("indicator", "income", "balance", "cash_flow")
        if financials.get(name) is None or financials[name].empty
    )
    if missing_inputs:
        return pd.DataFrame(), {"reason": "required_manifest_input_missing",
                                "missing_inputs": missing_inputs}

    panel = panel.copy()
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in panel:
            panel[column] = pd.to_numeric(panel[column], errors="coerce")
    panel = panel.dropna(subset=["trade_date", "symbol", "close"])
    panel = panel[~panel["quality_status"].isin({"unknown_unit", "failed"})]
    if panel.empty:
        return pd.DataFrame(), {"reason": "manifest_market_rows_unusable"}
    market_as_of = panel["trade_date"].max()
    history = panel.groupby("symbol")["trade_date"].nunique()
    market_input_mode = "manifest_dataset_ids"
    # Early materialized histories predate the append-only observation table, so
    # some otherwise valid manifests name only the recent slice.  Repair that
    # known manifest gap with the local warehouse locked to the manifest's market
    # as-of, then hash the exact consumed rows.  No post-cutoff market date enters.
    if int(history.max() or 0) < policy.minimum_history_days:
        repaired = store.load_daily_panel(
            market_as_of.date().isoformat(),
            trading_days=max(420, policy.minimum_history_days + 20), adjustment="qfq",
        )
        if repaired is None or repaired.empty:
            return pd.DataFrame(), {"reason": "manifest_history_incomplete"}
        repaired = repaired.copy()
        repaired["trade_date"] = pd.to_datetime(repaired["trade_date"], errors="coerce")
        repaired = repaired[
            repaired["trade_date"].notna()
            & (repaired["trade_date"] <= market_as_of)
            & ~repaired["quality_status"].isin({"unknown_unit", "failed"})
        ]
        panel = repaired
        history = panel.groupby("symbol")["trade_date"].nunique()
        market_input_mode = "manifest_as_of_warehouse_history_repair"
    current = set(panel.loc[panel["trade_date"] == market_as_of, "symbol"].astype(str))
    universe = universe.drop_duplicates("symbol").copy()
    universe["symbol"] = universe["symbol"].astype(str).map(_symbol)
    universe["history_days"] = universe["symbol"].map(history).fillna(0).astype(int)
    universe["has_current_bar"] = universe["symbol"].isin(current)

    feature_policy = SelectionPolicy(
        min_history_days=policy.minimum_history_days,
        min_listing_trading_days=policy.minimum_history_days,
        min_average_amount_20=policy.minimum_average_amount_20,
    )
    selector = LocalStockSelector(
        store=_FrozenManifestStore(financials=financials, events=events),
        policy=feature_policy,
    )
    eligible = selector._hard_gates(universe, panel, market_as_of)
    features = selector._liquidity_gates(selector._build_features(eligible, panel, market_as_of))
    if features.empty:
        return features, {"reason": "no_tradeable_security_passed_hard_gates"}
    valuation_fields = [column for column in (
        "symbol", "pe_ttm", "pb", "dividend_yield", "trade_date",
    ) if column in valuations]
    valuation_rows = valuations[valuation_fields].copy().drop_duplicates("symbol", keep="last")
    features = features.merge(valuation_rows, on="symbol", how="left", suffixes=("", "_valuation"))
    context = _context(manifest["decision_context"])
    fundamentals = selector._fundamentals(context)
    if str(fundamentals.attrs.get("revision_set_id") or "") != str(
        manifest.get("financial_revision_set_id") or ""
    ):
        return pd.DataFrame(), {"reason": "financial_revision_set_mismatch"}
    features = features.merge(fundamentals, on="symbol", how="left")
    features = selector._score_fundamentals(features)
    roe_columns = [
        column for column in (
            "roe", "indicator_roe", "indicator_roe_weighted", "indicator_roe_ttm",
        ) if column in features.columns
    ]
    features["roe"] = (
        features[roe_columns].apply(pd.to_numeric, errors="coerce").bfill(axis=1).iloc[:, 0]
        if roe_columns else np.nan
    )
    return features, {
        "manifest": manifest,
        "market_as_of": market_as_of.date().isoformat(),
        "universe_count": int(len(universe)),
        "hard_gate_count": int(len(eligible)),
        "feature_count": int(len(features)),
        "market_input_mode": market_input_mode,
        "market_input_hash": _frame_hash(panel, (
            "symbol", "trade_date", "open", "high", "low", "close", "volume", "amount",
            "turnover_rate", "is_paused", "is_st", "provider", "quality_status", "dataset_id",
        )),
        "market_dataset_ids": sorted({
            str(value) for value in panel.get("dataset_id", pd.Series(dtype=str)).dropna()
            if str(value).strip()
        }),
    }


def _score(frame: pd.DataFrame, policy: IndependentPolicy) -> tuple[pd.DataFrame, dict[str, int]]:
    work = frame.copy()
    for field in policy.required_fields:
        work[field] = pd.to_numeric(work.get(field), errors="coerce")
    missing_counts = {
        field: int(work[field].isna().sum()) for field in policy.required_fields
    }
    complete = work[list(policy.required_fields)].notna().all(axis=1)
    work = work[complete].copy()
    if work.empty:
        return work, missing_counts

    ranks = {
        "roe": _percentile(work["roe"]),
        "growth": _percentile(work["net_profit_growth_pct"]),
        "debt": _percentile(work["debt_ratio"], higher_is_better=False),
        "cash": _percentile(work["cash_quality"]),
        "return": _percentile(work["ret_60"]),
        "slope": _percentile(work["ma60_slope"]),
        "persistence": _percentile(work["persistence_60"]),
        "pe": _percentile(work["pe_ttm"].where(work["pe_ttm"] > 0), higher_is_better=False),
        "pb": _percentile(work["pb"].where(work["pb"] > 0), higher_is_better=False),
        "amount": _percentile(np.log1p(work["average_amount_20"].clip(lower=0))),
        "volume": _percentile(work["volume_20_vs_60"]),
        "amount_trend": _percentile(work["amount_20_vs_60"]),
        "drawdown": _percentile(work["max_drawdown_60"]),
        "volatility": _percentile(work["volatility_60"], higher_is_better=False),
    }
    work["fundamental_quality_score"] = (
        ranks["roe"] * 8 + ranks["growth"] * 8 + ranks["debt"] * 7 + ranks["cash"] * 7
    )
    work["medium_trend_score"] = (
        ranks["return"] * 9 + ranks["slope"] * 8 + ranks["persistence"] * 8
    )
    work["valuation_score"] = ranks["pe"] * 10 + ranks["pb"] * 10
    work["flow_liquidity_score"] = (
        ranks["amount"] * 5 + ranks["volume"] * 5 + ranks["amount_trend"] * 5
    )
    work["risk_discount_score"] = ranks["drawdown"] * 5 + ranks["volatility"] * 5
    components = [
        "fundamental_quality_score", "medium_trend_score", "valuation_score",
        "flow_liquidity_score", "risk_discount_score",
    ]
    work["total_score"] = work[components].sum(axis=1)
    return work.sort_values(["total_score", "symbol"], ascending=[False, True]), missing_counts


def _row(row: pd.Series, rank: int) -> dict[str, Any]:
    components = {
        "fundamental_quality": round(float(row["fundamental_quality_score"]), 6),
        "medium_trend": round(float(row["medium_trend_score"]), 6),
        "valuation": round(float(row["valuation_score"]), 6),
        "flow_liquidity": round(float(row["flow_liquidity_score"]), 6),
        "risk_discount": round(float(row["risk_discount_score"]), 6),
    }
    return {
        "rank": rank, "symbol": str(row["symbol"]), "name": str(row.get("name") or ""),
        "industry": str(row.get("industry") or ""),
        "total_score": round(float(row["total_score"]), 6),
        "score_components": components,
        "data_quality": {"required_dimensions_complete": True,
                         "required_field_count": len(IndependentPolicy().required_fields)},
        "tradeability": {"history_days": int(row.get("history_days") or 0),
                         "average_amount_20": round(float(row["average_amount_20"]), 2),
                         "paused_days_20": int(row.get("paused_days_20") or 0)},
    }


def build(manifest_id: str, *, store: ResearchStore | None = None,
          policy: IndependentPolicy | None = None) -> dict[str, Any]:
    """Build an independent result from immutable manifest inputs only."""
    store = store or ResearchStore(ensure_schema=False)
    policy = policy or IndependentPolicy()
    manifest = store.load_selection_manifest(manifest_id)
    if not manifest:
        return {"status": "unavailable", "reason": "manifest_missing",
                "strategy_version": policy.version, "strategy_hash": policy.policy_hash,
                "top15": [], "top5": []}
    frame, evidence = _prepare_frame(store, manifest_id, policy)
    if frame.empty:
        return {"status": "unavailable", **evidence,
                "strategy_version": policy.version, "strategy_hash": policy.policy_hash,
                "manifest_id": manifest_id, "top15": [], "top5": []}
    scored, missing_counts = _score(frame, policy)
    if len(scored) < 15:
        return {
            "status": "unavailable", "reason": "fewer_than_15_complete_eligible_rows",
            "strategy_version": policy.version, "strategy_hash": policy.policy_hash,
            "manifest_id": manifest_id, "market_as_of": evidence.get("market_as_of"),
            "eligible_complete_count": int(len(scored)), "missing_by_field": missing_counts,
            "top15": [], "top5": [],
        }
    rows = [_row(row, rank) for rank, (_, row) in enumerate(scored.head(15).iterrows(), 1)]
    snapshot_seed = {
        "strategy_version": policy.version, "strategy_hash": policy.policy_hash,
        "manifest_id": manifest_id, "market_as_of": evidence["market_as_of"],
        "market_input_hash": evidence["market_input_hash"],
        "market_dataset_ids": evidence["market_dataset_ids"],
        "financial_revision_set_id": manifest.get("financial_revision_set_id"),
        "event_dataset_id": manifest.get("event_dataset_id"),
        "valuation_dataset_ids": manifest.get("valuation_dataset_ids") or [],
    }
    return {
        "status": "ready", "strategy_id": STRATEGY_ID,
        "strategy_version": policy.version, "strategy_hash": policy.policy_hash,
        "manifest_id": manifest_id, "input_snapshot_id": _hash(snapshot_seed),
        "input_provenance": {
            "market_input_mode": evidence["market_input_mode"],
            "market_input_hash": evidence["market_input_hash"],
            "market_dataset_ids": evidence["market_dataset_ids"],
            "financial_revision_set_id": manifest.get("financial_revision_set_id"),
            "event_dataset_id": manifest.get("event_dataset_id"),
            "valuation_dataset_ids": manifest.get("valuation_dataset_ids") or [],
        },
        "selection_date": manifest["decision_context"]["selection_date"],
        "decision_at": manifest["decision_context"]["decision_at"],
        "market_as_of": evidence["market_as_of"],
        "weights": policy.public_dict()["component_weights"],
        "subweights": policy.public_dict()["subweights"],
        "required_fields": list(policy.required_fields),
        "universe_count": evidence["universe_count"],
        "hard_gate_count": evidence["hard_gate_count"],
        "eligible_complete_count": int(len(scored)),
        "missing_by_field": missing_counts,
        "top15": rows, "top5": rows[:5],
        "independence_boundary": (
            "immutable_manifest_inputs_only; formal_and_wencai_membership_rank_score_not_read"
        ),
    }


def build_and_persist(run_id: str, *, store: ResearchStore | None = None) -> dict[str, Any]:
    store = store or ResearchStore(ensure_schema=False)
    loader = getattr(store, "formal_selection", None)
    formal = loader(str(run_id or "")) if callable(loader) else None
    formal = formal or {}
    if str(formal.get("run_id") or "") != str(run_id or ""):
        return {"status": "unavailable", "reason": "formal_run_not_published",
                "top15": [], "top5": []}
    artifacts = formal.get("artifacts") or {}
    existing = artifact_payload(artifacts)
    if existing.get("status") == "ready":
        return existing
    manifest_id = str((formal.get("metadata") or {}).get("manifest_id") or "")
    result = build(manifest_id, store=store)
    artifact_type = REPAIR_ARTIFACT_TYPE if ARTIFACT_TYPE in artifacts else ARTIFACT_TYPE
    if artifact_type in artifacts:
        return existing
    if artifact_type == REPAIR_ARTIFACT_TYPE and result.get("status") != "ready":
        return result
    store.save_selection_artifact(str(run_id), artifact_type, result)
    if result.get("status") == "ready":
        nominations = [{
            "symbol": row["symbol"], "lane": "independent",
            "strategy_id": STRATEGY_ID, "strategy_version": result["strategy_version"],
            "lane_rank": row["rank"], "lane_score_raw": row["total_score"],
            "priority_weight": 0,
            "evidence": {"manifest_id": result["manifest_id"],
                         "snapshot_id": result["input_snapshot_id"],
                         "independent_from_formal_membership": True},
        } for row in result["top15"]]
        store.save_selection_strategy_records(
            str(run_id), nominations,
            policy={"version": result["strategy_version"], "policy_hash": result["strategy_hash"]},
            policy_hash=result["strategy_hash"], selection_date=result["market_as_of"],
            input_snapshot_id=result["input_snapshot_id"], persist_policy=False,
        )
    return result


def comparison(formal_rows: Iterable[dict], independent: dict,
               wencai_runs: dict) -> dict[str, Any]:
    """Compare only sets whose own inputs are valid; never impute failed sources."""
    formal = {_symbol(row.get("symbol") or row.get("code")) for row in formal_rows or ()}
    formal.discard("")
    independent_ready = independent.get("status") == "ready"
    independent_set = {
        _symbol(row.get("symbol") or row.get("code"))
        for row in (independent.get("top15") or [])
    } if independent_ready else set()
    strategies = (wencai_runs or {}).get("strategies") or {}
    wencai_ready = (
        EXPECTED_WENCAI_STRATEGIES.issubset(strategies)
        and all((strategies.get(name) or {}).get("status") == "ready"
                for name in EXPECTED_WENCAI_STRATEGIES)
    )
    wencai = {
        _symbol(row.get("symbol") or row.get("code"))
        for name, value in strategies.items() if name in EXPECTED_WENCAI_STRATEGIES
        for row in (value.get("picks") or [])
    } if wencai_ready else set()
    result: dict[str, Any] = {
        "availability": {"formal": bool(formal), "independent": independent_ready,
                         "wencai": wencai_ready},
        "pairwise": {}, "triple": None,
    }

    def pair(left_name: str, left: set[str], right_name: str, right: set[str]) -> dict:
        return {"intersection": sorted(left & right),
                f"{left_name}_only": sorted(left - right),
                f"{right_name}_only": sorted(right - left)}

    if formal and independent_ready:
        result["pairwise"]["formal_independent"] = pair(
            "formal", formal, "independent", independent_set
        )
    if formal and wencai_ready:
        result["pairwise"]["formal_wencai"] = pair("formal", formal, "wencai", wencai)
    if independent_ready and wencai_ready:
        result["pairwise"]["independent_wencai"] = pair(
            "independent", independent_set, "wencai", wencai
        )
    if formal and independent_ready and wencai_ready:
        result["triple"] = {
            "intersection": sorted(formal & independent_set & wencai),
            "formal_only": sorted(formal - independent_set - wencai),
            "independent_only": sorted(independent_set - formal - wencai),
            "wencai_only": sorted(wencai - formal - independent_set),
        }
    return result
