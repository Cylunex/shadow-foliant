from copy import deepcopy

import pytest

from analysis.shortlist_attribution import attribute_shortlist


def fixture():
    rows, artifacts = [], {"run": {"formal_top5": [], "formal_top15": []}}
    for i in range(15):
        symbol = f"600{i:03d}"
        item = {"symbol": symbol, "run_id": "run", "selection_date": "2026-10-20",
                "assigned_lane": ["core", "satellite", "timing"][i % 3], "industry": f"industry{i % 4}",
                "rule_version": "v2", "policy_hash": "policy", "manifest_id": "manifest", "snapshot_id": "snapshot"}
        for size in (5, 15):
            if i >= size:
                continue
            sid = f"formal_local_fusion_top{size}"
            artifacts["run"][f"formal_top{size}"].append(item.copy())
            rows.append({"nomination_id": f"{size}-{symbol}", "selection_run_id": "run",
                         "strategy_id": sid, "strategy_version": "v2", "lane": "formal",
                         "symbol": symbol, "selection_date": "2026-10-20", "horizon_days": 1,
                         "outcome_status": "matured", "entry_date": "2026-10-21", "exit_date": "2026-10-21",
                         "metric_version": "price-v2", "return_pct": float(i - 7)})
    return rows, artifacts


def analyze(rows, artifacts):
    return attribute_shortlist(rows, artifacts, start_date="2026-10-17", end_date="2026-10-23",
                               as_of="2026-10-23")


def test_attribution_reconciles_exactly_and_retains_missing_score_evidence():
    rows, artifacts = fixture()
    result = analyze(rows, artifacts)
    assert result["mean_gap_pp"] == -5.0
    cohort = result["cohorts"][0]
    assert sum(r["gap_contribution_pp"] for r in cohort["stocks"]) == pytest.approx(-5)
    assert cohort["decomposition_residual_pp"] == pytest.approx(0)
    assert cohort["other10_return_pct"] == 2.5
    assert cohort["score_rank_ic"] is None
    assert result["promotion_evidence"] is result["causal_claim"] is False


@pytest.mark.parametrize("damage,reason", [
    ("future", "incomplete_labels"), ("different_return", "same_stock_label_mismatch"),
    ("missing_member", "invalid_frozen_membership"), ("duplicate_member", "invalid_frozen_membership"),
    ("different_snapshot", "unmatched_frozen_inputs"), ("wrong_version", "invalid_frozen_membership"),
    ("outside_universe", "top5_not_subset_of_top15"), ("no_artifact", "missing_frozen_memberships"),
])
def test_invalid_evidence_cannot_explain_a_performance_gap(damage, reason):
    rows, artifacts = fixture()
    if damage == "future":
        rows[0]["exit_date"] = "2026-10-26"
    elif damage == "different_return":
        rows[0]["return_pct"] += 1
    elif damage == "missing_member":
        artifacts["run"]["formal_top5"].pop()
    elif damage == "duplicate_member":
        artifacts["run"]["formal_top5"][0] = artifacts["run"]["formal_top5"][1]
    elif damage == "different_snapshot":
        artifacts["run"]["formal_top5"][0]["snapshot_id"] = "other"
    elif damage == "wrong_version":
        artifacts["run"]["formal_top5"][0]["rule_version"] = "other"
    elif damage == "outside_universe":
        artifacts["run"]["formal_top5"][0]["symbol"] = "999999"
    elif damage == "no_artifact":
        artifacts.clear()
    result = analyze(rows, artifacts)
    assert result["mean_gap_pp"] is None
    assert result["excluded_cohorts"][0]["reason"] == reason


def test_frozen_full_scores_enable_rank_ic_and_cohorts_remain_equal_weight():
    rows, artifacts = fixture()
    artifacts["run"]["top5_shortlist_audit"] = {"candidates": [
        {"symbol": f"600{i:03d}", "shortlist_score": -float(i)} for i in range(15)]}
    result = analyze(rows, artifacts)
    assert result["cohorts"][0]["score_rank_ic"] == pytest.approx(-1)
    second = deepcopy(rows)
    for row in second:
        row.update(selection_run_id="run2", nomination_id="next-" + row["nomination_id"],
                   selection_date="2026-10-21", entry_date="2026-10-22", exit_date="2026-10-22",
                   return_pct=0.0)
    artifacts["run2"] = deepcopy(artifacts["run"])
    for kind in ("formal_top5", "formal_top15"):
        for row in artifacts["run2"][kind]:
            row.update(run_id="run2", selection_date="2026-10-21")
    result = analyze(rows + second, artifacts)
    assert result["mean_gap_pp"] == -2.5
    assert sum(result["mean_symbol_contribution_pp"].values()) == pytest.approx(-2.5)
    assert result["unique_top5_symbols"] == 5


def test_shadow_is_compared_only_when_same_window_and_membership_mature():
    rows, artifacts = fixture()
    shadow = [deepcopy(r) for r in rows if r['strategy_id'].endswith('top15') and int(r['symbol']) >= 600010]
    for row in shadow:
        row.update(strategy_id='shadow_top5_score_caps', strategy_version='score-caps-v1', lane='shadow',
                   nomination_id='shadow-' + row['nomination_id'])
    artifacts['run']['top5_shortlist_audit'] = {'challenger': {
        'strategy_id': 'shadow_top5_score_caps', 'strategy_version': 'score-caps-v1',
        'status': 'complete', 'affects_formal_selection': False, 'rows': shadow}}
    result = analyze(rows + shadow, artifacts)
    assert result['shadow_complete_pair_count'] == 1
    assert result['shadow_mean_delta_vs_top5_pp'] == 10
    for row in shadow:
        row['selection_date'] = '2026-10-19'
    assert analyze(rows + shadow, artifacts)['shadow_complete_pair_count'] == 0
    for row in shadow:
        row['selection_date'] = '2026-10-20'
    artifacts['run']['top5_shortlist_audit']['challenger']['rows'] = shadow + [shadow[0]]
    assert analyze(rows + shadow, artifacts)['shadow_complete_pair_count'] == 0
    artifacts['run']['top5_shortlist_audit']['challenger']['rows'] = shadow
    shadow[0]['return_pct'] += 2
    assert analyze(rows + shadow, artifacts)['shadow_complete_pair_count'] == 0
