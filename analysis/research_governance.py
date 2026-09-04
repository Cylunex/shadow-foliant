"""Pure experiment/evidence contracts. LLM proposals cannot bypass these gates."""
from __future__ import annotations
from datetime import date, timedelta
import math
from application.results import payload_hash

HYPOTHESES = {
    "earnings_quality": {"baseline": "local_pit_v4", "metric": "cost_adjusted_excess",
                         "fields": ["operating_cash_flow", "net_profit"], "trial_budget": 8},
    "capital_persistence": {"baseline": "local_main_force", "metric": "cost_adjusted_excess",
                            "fields": ["main_net_inflow", "amount"], "trial_budget": 8},
    "trend_exhaustion": {"baseline": "fusion", "metric": "risk_adjusted_excess",
                         "fields": ["close", "volume"], "trial_budget": 8},
}


def purge_training_intervals(training, validation, *, embargo_days=5):
    """Purge whole label intervals, not just decision-day equality."""
    blocked = [(date.fromisoformat(r["label_start"]) - timedelta(days=embargo_days),
                date.fromisoformat(r["label_end"]) + timedelta(days=embargo_days)) for r in validation]
    return [r for r in training if all(
        date.fromisoformat(r["label_end"]) < start or date.fromisoformat(r["label_start"]) > end
        for start, end in blocked)]


def evidence_summary(rows, *, trials_attempted=1, block_days=5):
    """One mean per entry day; greedy disjoint label intervals cap independence."""
    by_date, versions = {}, set()
    for row in rows:
        if row.get("status") != "matured" or row.get("data_class") != "strict_observed_pit":
            continue
        value = row.get("net_excess_return_pct")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        versions.add(str(row["strategy_version"]))
        key = row["label_start"]
        item = by_date.setdefault(key, {"values": {}, "end": row["label_end"]})
        item["values"][str(row["symbol"])] = value  # repeated nomination is not new evidence
        item["end"] = max(item["end"], row["label_end"])
    independent, last_end = [], ""
    for start, item in sorted(by_date.items()):
        if start > last_end:
            independent.append(sum(item["values"].values()) / len(item["values"]))
            last_end = item["end"]
    n = len(independent)
    mean = sum(independent) / n if n else None
    variance = sum((v - mean) ** 2 for v in independent) / (n - 1) if n > 1 else None
    # Conservative Bonferroni-style normal threshold; never claim this establishes
    # causality or solves all dependence. Require 20 non-overlapping intervals.
    threshold = math.sqrt(2 * math.log(max(1, trials_attempted) / .025))
    lower = mean - threshold * math.sqrt(variance / n) if variance is not None else None
    # Dependence is conservatively summarized in contiguous date blocks. This is
    # not a proof of independence; it prevents overlapping/adjacent entries from
    # being advertised as independent observations.
    blocks = [sum(independent[i:i + block_days]) / len(independent[i:i + block_days])
              for i in range(0, len(independent), block_days)]
    bn = len(blocks)
    bmean = sum(blocks) / bn if bn else None
    bvariance = sum((v - bmean) ** 2 for v in blocks) / (bn - 1) if bn > 1 else None
    robust_lower = bmean - threshold * math.sqrt(bvariance / bn) if bvariance is not None else None
    result = {"evidence_version": "date-block-net-v2", "entry_dates": len(by_date),
              "effective_samples": n, "strategy_versions": sorted(versions),
              "nonoverlapping_samples": n,
              "date_blocks": bn, "block_days": block_days, "independence_claimed": False,
              "trials_attempted": trials_attempted, "mean_net_excess_pct": mean,
              "conservative_lower_bound_pct": robust_lower,
              "status": "sufficient" if n >= 20 and bn >= 4 and len(versions) == 1 else "insufficient_evidence",
              "promotion_ready": n >= 20 and bn >= 4 and len(versions) == 1 and robust_lower is not None and robust_lower > 0}
    result["evidence_snapshot_id"] = payload_hash({"summary": result, "dated_values": by_date})
    return result


def rolling_validation(rows, *, train_days=120, validation_days=20, embargo_days=5):
    days = sorted({r["label_start"] for r in rows})
    folds = []
    for offset in range(train_days, len(days), validation_days):
        test_days = set(days[offset:offset + validation_days])
        train_dates = set(days[max(0, offset - train_days):offset])
        validation = [r for r in rows if r["label_start"] in test_days]
        training = [r for r in rows if r["label_start"] in train_dates]
        folds.append({"train": purge_training_intervals(training, validation, embargo_days=embargo_days),
                      "validation": validation})
    return folds
