"""Typed validation protocol and deterministic search accounting."""
from dataclasses import asdict, dataclass
from datetime import date, datetime
import math
from zoneinfo import ZoneInfo
from application.results import payload_hash


@dataclass(frozen=True)
class ValidationProtocol:
    protocol_id: str
    metric: str
    label_horizon_days: int
    cost_model: str
    split_method: str = "purged_walk_forward"
    purge_days: int = 5
    block_days: int = 5
    minimum_effective_samples: int = 20
    benchmark: str = "registered_baseline"

    def __post_init__(self):
        if not self.protocol_id or self.metric not in {"cost_adjusted_excess", "risk_adjusted_excess"}:
            raise ValueError("invalid_validation_protocol")
        if self.label_horizon_days < 1 or self.purge_days < self.label_horizon_days or self.block_days < 1:
            raise ValueError("unsafe_validation_boundaries")
        if self.minimum_effective_samples < 20 or not self.cost_model:
            raise ValueError("validation_evidence_floor")
        if self.split_method != "purged_walk_forward":
            raise ValueError("unsupported_validation_split")

    @property
    def fingerprint(self):
        return payload_hash(asdict(self))


DEFAULT_PROTOCOL = ValidationProtocol("net-forward-v2", "cost_adjusted_excess", 5, "a-share-next-open-cost-v1")


def search_counts(*, attempts, candidates, metric_views, holdout_accesses):
    values = {"attempts": attempts, "candidates": candidates, "metric_views": metric_views,
              "holdout_accesses": holdout_accesses}
    if any(type(v) is not int or v < 0 for v in values.values()) or candidates > attempts:
        raise ValueError("invalid_search_counts")
    return values


def evaluate_rows(rows, protocol=DEFAULT_PROTOCOL):
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("bounded_evaluation_rows_required")
    result = []
    for row in rows:
        if any(k not in row for k in ("symbol", "label_start", "label_end", "gross_return_pct", "cost_pct", "baseline_return_pct")):
            raise ValueError("evaluation_row_incomplete")
        start, end = date.fromisoformat(row["label_start"]), date.fromisoformat(row["label_end"])
        numeric = [row[k] for k in ("gross_return_pct", "cost_pct", "baseline_return_pct")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in numeric) or row["cost_pct"] < 0:
            raise ValueError("invalid_evaluation_numbers")
        if row.get("data_class") == "strict_observed_pit":
            if not row.get("input_receipt_id") or not row.get("execution_fact_ids"):
                raise ValueError("strict_evaluation_receipts_required")
            try:
                observed = datetime.fromisoformat(row["input_observed_at"])
                decision = datetime.fromisoformat(row["decision_at"])
                executed = datetime.fromisoformat(row["execution_at"])
                if (observed.tzinfo is None or decision.tzinfo is None or executed.tzinfo is None
                        or observed > decision or decision > executed
                        or executed.astimezone(ZoneInfo("Asia/Shanghai")).date() != start):
                    raise ValueError("lookahead_in_evaluation")
            except KeyError as exc:
                raise ValueError("evaluation_time_evidence_required") from exc
        if (end - start).days < protocol.label_horizon_days:
            raise ValueError("label_horizon_too_short")
        value = row["gross_return_pct"] - row["cost_pct"] - row["baseline_return_pct"]
        if protocol.metric == "risk_adjusted_excess":
            drawdowns = [row.get(k) for k in ("max_drawdown_pct", "baseline_max_drawdown_pct")]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not -100 <= v <= 0 for v in drawdowns):
                raise ValueError("risk_metric_drawdown_evidence_required")
            # Fixed percentage-point penalty, not a tunable evaluator-supplied
            # bonus: excess drawdown relative to the registered baseline.
            penalty = max(0, abs(drawdowns[0]) - abs(drawdowns[1]))
            value -= penalty
        result.append({**row, "net_excess_return_pct": value, "status": "matured",
                       "protocol_id": protocol.protocol_id, "protocol_hash": protocol.fingerprint})
    return result


def validate_trial_rows(rows, trial):
    protocol = ValidationProtocol(**trial["validation_protocol"])
    if trial["data_class"] == "strict_observed_pit":
        for row in rows:
            expected = {"data_class": "strict_observed_pit", "trial_id": trial["trial_id"],
                        "dataset_id": trial["dataset_id"], "code_revision": trial["code_revision"],
                        "cost_model": protocol.cost_model, "strategy_version": trial["formula_hash"]}
            if any(row.get(k) != v for k, v in expected.items()):
                raise ValueError("evaluation_trial_binding_mismatch")
    return evaluate_rows(rows, protocol)
