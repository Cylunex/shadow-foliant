"""Descriptive, matched-cohort shortlist attribution; never promotion evidence."""
from collections import Counter, defaultdict
from math import isfinite
from statistics import mean

from analysis.selection_performance import summarize_selection_performance

TOP5 = "formal_local_fusion_top5"
TOP15 = "formal_local_fusion_top15"


def attribute_shortlist(rows, artifacts, *, start_date, end_date, as_of, horizon_days=1):
    """Join immutable membership to complete labels before explaining any gap.

    Allocation and within-lane selection are an arithmetic decomposition, not
    causal estimates. Each date/run gets equal weight; overlapping stocks and
    horizons do not supply independent statistical observations.
    """
    summary = summarize_selection_performance(
        rows, start_date=start_date, end_date=end_date, as_of=as_of, horizon_days=horizon_days)
    by_run = defaultdict(dict)
    for cohort in summary["daily"]:
        if cohort["strategy_id"] in {TOP5, TOP15}:
            by_run[(cohort["selection_run_id"], cohort["strategy_version"])][cohort["strategy_id"]] = cohort
    labels = {(r["selection_run_id"], r["strategy_version"], r["strategy_id"], r["symbol"]): r
              for r in rows}
    complete, excluded = [], []
    for (run_id, version), pair in sorted(by_run.items()):
        five, fifteen = pair.get(TOP5), pair.get(TOP15)
        reason = None
        if not five or not fifteen or five["status"] != "complete" or fifteen["status"] != "complete":
            reason = "incomplete_labels"
        elif (five["nomination_count"], fifteen["nomination_count"]) != (5, 15):
            reason = "unexpected_cohort_size"
        elif (five["intervals"] != fifteen["intervals"] or five["metric_versions"] != fifteen["metric_versions"]
              or five["selection_date"] != fifteen["selection_date"]):
            reason = "unmatched_observation_window"
        frozen = artifacts.get(run_id, {})
        members = {key: frozen.get(key) for key in ("formal_top5", "formal_top15")}
        if not reason and any(not isinstance(members[key], list) for key in members):
            reason = "missing_frozen_memberships"
        maps = {}
        if not reason:
            for kind, expected in (("formal_top5", 5), ("formal_top15", 15)):
                source = members[kind]
                maps[kind] = {r.get("symbol") or r.get("code"): r for r in source if isinstance(r, dict)}
                if (len(source) != expected or len(maps[kind]) != expected or not all(maps[kind])
                        or any(r.get("run_id") != run_id or r.get("selection_date") != five["selection_date"]
                               or r.get("rule_version") != version
                               for r in maps[kind].values())):
                    reason = "invalid_frozen_membership"
            identities = {(r.get("policy_hash"), r.get("manifest_id"), r.get("snapshot_id"))
                          for source in maps.values() for r in source.values()}
            if len(identities) != 1 or any(not all(identity) for identity in identities):
                reason = "unmatched_frozen_inputs"
            if not reason and not maps["formal_top5"].keys() <= maps["formal_top15"].keys():
                reason = "top5_not_subset_of_top15"
        if not reason:
            for kind, sid in (("formal_top5", TOP5), ("formal_top15", TOP15)):
                observed = {r["symbol"] for r in rows if (r["selection_run_id"], r["strategy_version"], r["strategy_id"])
                            == (run_id, version, sid)}
                if observed != maps[kind].keys():
                    reason = "labels_membership_mismatch"
            for symbol in maps["formal_top5"]:
                a, b = labels.get((run_id, version, TOP5, symbol)), labels.get((run_id, version, TOP15, symbol))
                if a and b and abs(a["return_pct"] - b["return_pct"]) > 1e-6:
                    reason = "same_stock_label_mismatch"
        if reason:
            excluded.append({"selection_run_id": run_id, "reason": reason})
            continue
        selected = set(maps["formal_top5"])
        stocks = []
        for symbol, membership in maps["formal_top15"].items():
            label = labels[(run_id, version, TOP15, symbol)]
            chosen = symbol in selected
            stocks.append({"symbol": symbol, "selected": chosen,
                           "lane": membership.get("assigned_lane") or "unknown",
                           "industry": membership.get("industry") or "未分类",
                           "return_pct": label["return_pct"],
                           "gap_contribution_pp": ((1 / 5 if chosen else 0) - 1 / 15) * label["return_pct"]})
        lanes = []
        for lane in sorted({r["lane"] for r in stocks}):
            all_lane = [r for r in stocks if r["lane"] == lane]
            chosen = [r for r in all_lane if r["selected"]]
            rest = [r for r in all_lane if not r["selected"]]
            base_mean = mean(r["return_pct"] for r in all_lane)
            selected_mean = mean(r["return_pct"] for r in chosen) if chosen else None
            w5, w15 = len(chosen) / 5, len(all_lane) / 15
            lanes.append({"lane": lane, "top5_count": len(chosen), "top15_count": len(all_lane),
                          "top5_return_pct": selected_mean, "top15_return_pct": base_mean,
                          "other_return_pct": mean(r["return_pct"] for r in rest) if rest else None,
                          "allocation_pp": (w5 - w15) * base_mean,
                          "within_lane_selection_pp": w5 * (selected_mean - base_mean) if chosen else 0.0})
        score_ic = None
        audit = frozen.get("top5_shortlist_audit") or {}
        scored = audit.get("candidates") or []
        scores = {r.get("symbol"): r.get("shortlist_score") for r in scored if isinstance(r, dict)}
        if (len(scored) == 15 and scores.keys() == maps["formal_top15"].keys()
                and all(type(v) in (float, int) and isfinite(v) for v in scores.values())):
            import pandas as pd
            x = pd.Series([scores[r["symbol"]] for r in stocks]).rank()
            y = pd.Series([r["return_pct"] for r in stocks]).rank()
            if x.nunique() > 1 and y.nunique() > 1:
                score_ic = float(x.corr(y))
        top5_mean = mean(r["return_pct"] for r in stocks if r["selected"])
        top15_mean = mean(r["return_pct"] for r in stocks)
        gap = top5_mean - top15_mean
        challenger = audit.get("challenger") or {}
        shadow = {"status": "not_recorded", "delta_vs_top5_pp": None}
        if challenger:
            shadow = {"status": "incomplete_or_unmatched", "delta_vs_top5_pp": None}
            sid, sv = challenger.get("strategy_id"), challenger.get("strategy_version")
            match = next((c for c in summary["daily"] if c["selection_run_id"] == run_id
                          and c["strategy_id"] == sid and c["strategy_version"] == sv), None)
            expected_symbols = {r.get("symbol") for r in challenger.get("rows", [])}
            observed = {r["symbol"]: r for r in rows if (r["selection_run_id"], r["strategy_id"], r["strategy_version"])
                        == (run_id, sid, sv)}
            if (challenger.get("status") == "complete" and challenger.get("affects_formal_selection") is False
                    and match and match["status"] == "complete" and match["nomination_count"] == 5
                    and match["selection_date"] == five["selection_date"]
                    and match["intervals"] == five["intervals"] and match["metric_versions"] == five["metric_versions"]
                    and len(challenger.get("rows", [])) == len(expected_symbols) == 5
                    and observed.keys() == expected_symbols
                    and expected_symbols <= maps["formal_top15"].keys()
                    and all(abs(r["return_pct"] - labels[(run_id, version, TOP15, symbol)]["return_pct"]) <= 1e-6
                            for symbol, r in observed.items())):
                shadow = {"status": "complete", "delta_vs_top5_pp": mean(r["return_pct"] for r in observed.values()) - top5_mean,
                          "strategy_id": sid, "strategy_version": sv, "promotion_evidence": False}
        complete.append({"selection_run_id": run_id, "selection_date": five["selection_date"],
                         "strategy_version": version, "interval": five["intervals"][0],
                         "top5_return_pct": top5_mean, "top15_return_pct": top15_mean,
                         "other10_return_pct": mean(r["return_pct"] for r in stocks if not r["selected"]),
                         "gap_pp": gap, "lanes": lanes,
                         "challenger_comparison": shadow,
                         "stocks": sorted(stocks, key=lambda r: (r["gap_contribution_pp"], r["symbol"])),
                         "score_rank_ic": score_ic,
                         "score_rank_ic_status": "available" if score_ic is not None else "missing_or_constant_scores",
                         "decomposition_residual_pp": gap - sum(r["allocation_pp"] + r["within_lane_selection_pp"] for r in lanes),
                         "top5_industry_counts": dict(Counter(r["industry"] for r in stocks if r["selected"]))})
    lane_totals, symbol_totals = defaultdict(lambda: defaultdict(float)), defaultdict(float)
    for cohort in complete:
        for lane in cohort["lanes"]:
            for field in ("allocation_pp", "within_lane_selection_pp"):
                lane_totals[lane["lane"]][field] += lane[field] / len(complete)
        for stock in cohort["stocks"]:
            symbol_totals[stock["symbol"]] += stock["gap_contribution_pp"] / len(complete)
    shadow_pairs = [c["challenger_comparison"]["delta_vs_top5_pp"] for c in complete
                    if c["challenger_comparison"]["status"] == "complete"]
    return {"status": "available" if complete else "insufficient_data",
            "complete_cohort_count": len(complete), "excluded_cohorts": excluded,
            "mean_gap_pp": mean(r["gap_pp"] for r in complete) if complete else None,
            "shadow_complete_pair_count": len(shadow_pairs),
            "shadow_mean_delta_vs_top5_pp": mean(shadow_pairs) if shadow_pairs else None,
            "mean_lane_attribution": dict(lane_totals),
            "mean_symbol_contribution_pp": dict(sorted(symbol_totals.items(), key=lambda item: (item[1], item[0]))),
            "unique_top5_symbols": len({r["symbol"] for c in complete for r in c["stocks"] if r["selected"]}),
            "cohorts": complete, "causal_claim": False, "promotion_evidence": False,
            "limitations": ["price_labels_without_execution_costs", "overlapping_cohorts_not_independent",
                            "allocation_and_selection_are_arithmetic_not_causal",
                            "missing_scores_never_reconstructed_from_current_data"]}
