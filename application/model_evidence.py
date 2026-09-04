"""Policy-bound net evidence from comparable, contemporaneous model ledgers.

An indicative NAV, missing calendar session or crossing policy version is not
promotion evidence. No price-label fallback is permitted here.
"""
import json
from analysis.research_governance import evidence_summary, rolling_validation
from application.results import payload_hash


def model_strategy_evidence(store, policy_hash, *, horizon_days=5):
    if type(horizon_days) is not int or not 1 <= horizon_days <= 20:
        raise ValueError("invalid_evidence_horizon")
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT baseline,payload FROM research_model_portfolios")
        ledgers = {baseline: json.loads(raw) for baseline, raw in cur.fetchall()}
        cur.execute("SELECT trade_date FROM research_trade_calendar ORDER BY trade_date")
        calendar = [str(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()
    baseline = {r["trade_date"]: r for r in ledgers.get("pit_only", {}).get("marks", [])}
    results = []
    for name, ledger in ledgers.items():
        if not name.startswith("strategy:"):
            continue
        marks = {r["trade_date"]: r for r in ledger["marks"]}
        observations = []
        for offset in range(horizon_days, len(calendar)):
            window = calendar[offset - horizon_days:offset + 1]
            both = [book.get(day) for book in (marks, baseline) for day in window]
            if not all(r and r.get("status") == "verified" and r.get("policy_hash") == policy_hash
                       and r.get("net_asset_value") and float(r["net_asset_value"]) > 0 for r in both):
                continue
            start, end = window[0], window[-1]
            excess = ((float(marks[end]["net_asset_value"]) / float(marks[start]["net_asset_value"]) - 1) -
                      (float(baseline[end]["net_asset_value"]) / float(baseline[start]["net_asset_value"]) - 1)) * 100
            observations.append({"status": "matured", "data_class": "strict_observed_pit",
                                 "strategy_version": policy_hash, "symbol": name,
                                 "label_start": start, "label_end": end, "net_excess_return_pct": excess})
        summary = evidence_summary(observations, trials_attempted=max(1, len(ledgers)))
        folds = rolling_validation(observations, train_days=40, validation_days=20)
        fold_results = [evidence_summary(f["validation"], trials_attempted=max(1, len(ledgers))) for f in folds]
        # Independence and positive lower bound are necessary but not sufficient:
        # require out-of-time folds as well, not a single in-sample winning run.
        summary["promotion_ready"] = summary["promotion_ready"] and bool(fold_results) and all(
            r["mean_net_excess_pct"] is not None and r["mean_net_excess_pct"] > 0 for r in fold_results)
        summary.update(strategy_id=name.split(":", 1)[1], evidence_kind="executable_net",
                       evidence_policy_hash=policy_hash, rolling_folds=fold_results,
                       blocker=None if summary["promotion_ready"] else "net_evidence_or_rolling_validation_not_ready")
        results.append(summary)
    return {"strategies": results, "evidence_snapshot_id": payload_hash(results),
            "source": "forward_verified_model_accounts", "horizon_days": horizon_days}
