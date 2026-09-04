"""Typed validation protocol and deterministic search accounting."""
from dataclasses import asdict, dataclass
from datetime import date, datetime
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
    result = []
    for row in rows:
        if any(k not in row for k in ("symbol", "label_start", "label_end", "gross_return_pct", "cost_pct", "baseline_return_pct")):
            raise ValueError("evaluation_row_incomplete")
        start, end = date.fromisoformat(row["label_start"]), date.fromisoformat(row["label_end"])
        if row.get("data_class") == "strict_observed_pit":
            if not row.get("input_receipt_id") or not row.get("execution_fact_ids"):
                raise ValueError("strict_evaluation_receipts_required")
            try:
                observed = datetime.fromisoformat(row["input_observed_at"])
                decision = datetime.fromisoformat(row["decision_at"])
                if observed.tzinfo is None or decision.tzinfo is None or observed > decision or decision.date() > start:
                    raise ValueError("lookahead_in_evaluation")
            except KeyError as exc:
                raise ValueError("evaluation_time_evidence_required") from exc
        if (end - start).days < protocol.label_horizon_days:
            raise ValueError("label_horizon_too_short")
        value = row["gross_return_pct"] - row["cost_pct"] - row["baseline_return_pct"]
        result.append({**row, "net_excess_return_pct": value, "status": "matured",
                       "protocol_id": protocol.protocol_id, "protocol_hash": protocol.fingerprint})
    return result
