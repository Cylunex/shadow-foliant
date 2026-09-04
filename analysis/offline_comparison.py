"""Compare exported offline-model results without importing their runtimes.

Only JSON scalar observations are accepted. No model pickle, Python, callback,
URI dereference or automatic policy activation. The producer must supply net
labels under the same execution contract; hashes bind the comparison inputs.
"""
from datetime import date
import math
from analysis.research_governance import evidence_summary, rolling_validation
from application.results import payload_hash


def compare_exports(payload, *, sealed_intervals=()):
    if not isinstance(payload, dict) or not payload.get("dataset_id") or not payload.get("execution_model"):
        raise ValueError("offline_provenance_required")
    exports = payload.get("exports")
    if not isinstance(exports, list) or not 2 <= len(exports) <= 4:
        raise ValueError("offline_comparison_requires_two_to_four_exports")
    allowed = {"factor_ast", "qlib_offline", "rd_agent_offline", "learning_rank_offline"}
    keyed, versions = {}, {}
    for export in exports:
        adapter = export.get("adapter")
        if adapter not in allowed or adapter in keyed or not export.get("code_revision"):
            raise ValueError("offline_adapter_or_revision_invalid")
        if export.get("dataset_id") != payload["dataset_id"] or export.get("execution_model") != payload["execution_model"]:
            raise ValueError("offline_comparison_contract_mismatch")
        rows = export.get("observations")
        if not isinstance(rows, list) or len(rows) > 20000:
            raise ValueError("offline_observation_budget")
        keyed[adapter] = {}
        versions[adapter] = export["code_revision"]
        for row in rows:
            start, end = row["label_start"], row["label_end"]
            if date.fromisoformat(start) > date.fromisoformat(end):
                raise ValueError("invalid_label_interval")
            if any(start <= right and end >= left for left, right in sealed_intervals):
                raise ValueError("sealed_holdout_in_research_export")
            value = row.get("net_excess_return_pct")
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError("non_finite_offline_label")
            key = (str(row["symbol"]), start, end)
            if key in keyed[adapter]:
                raise ValueError("duplicate_offline_observation")
            keyed[adapter][key] = {**row, "strategy_version": export["code_revision"]}
    common = set.intersection(*(set(rows) for rows in keyed.values()))
    if not common:
        raise ValueError("no_common_offline_observations")
    output = []
    for adapter, rows in keyed.items():
        paired = [rows[key] for key in sorted(common)]
        summary = evidence_summary(paired, trials_attempted=len(exports))
        folds = rolling_validation(paired)
        summary.update(adapter=adapter, code_revision=versions[adapter], paired_rows=len(paired),
                       excluded_unpaired_rows=len(rows) - len(paired),
                       rolling_folds=[{"training_rows": len(f["train"]),
                                       "validation": evidence_summary(f["validation"], trials_attempted=len(exports))}
                                      for f in folds],
                       promotion_ready=False, promotion_blocker="offline_export_requires_independent_forward_validation")
        output.append(summary)
    return {"schema_version": "offline-model-comparison-v1", "dataset_id": payload["dataset_id"],
            "input_hash": payload_hash(payload), "models": output, "promotion_ready": False,
            "warning": "外部结果配对比较，不证明导出模型无泄漏，不自动进入正式选股"}
