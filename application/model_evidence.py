"""Policy-bound net evidence from comparable, contemporaneous model ledgers.

An indicative NAV, missing calendar session or crossing policy version is not
promotion evidence. No price-label fallback is permitted here.
"""
import json
from analysis.research_governance import evidence_summary, rolling_validation
from application.results import payload_hash


def _ledger_summary(*, name, ledger, baseline, calendar, policy_hash,
                    horizon_days, trials_attempted):
    marks = {row["trade_date"]: row for row in ledger.get("marks", [])}
    observations = []
    for offset in range(horizon_days, len(calendar)):
        window = calendar[offset - horizon_days:offset + 1]
        paired = [book.get(day) for book in (marks, baseline) for day in window]
        if not all(
            row and row.get("status") == "verified"
            and row.get("policy_hash") == policy_hash
            and row.get("net_asset_value")
            and float(row["net_asset_value"]) > 0
            for row in paired
        ):
            continue
        start, end = window[0], window[-1]
        excess = (
            (float(marks[end]["net_asset_value"])
             / float(marks[start]["net_asset_value"]) - 1)
            - (float(baseline[end]["net_asset_value"])
               / float(baseline[start]["net_asset_value"]) - 1)
        ) * 100
        observations.append({
            "status": "matured", "data_class": "strict_observed_pit",
            "strategy_version": policy_hash, "symbol": name,
            "label_start": start, "label_end": end,
            "net_excess_return_pct": excess,
        })
    summary = evidence_summary(observations, trials_attempted=trials_attempted)
    folds = rolling_validation(observations, train_days=40, validation_days=20)
    fold_results = [
        evidence_summary(fold["validation"], trials_attempted=trials_attempted)
        for fold in folds
    ]
    summary["promotion_ready"] = summary["promotion_ready"] and bool(fold_results) and all(
        row["mean_net_excess_pct"] is not None
        and row["mean_net_excess_pct"] > 0
        for row in fold_results
    )
    summary.update({
        "evidence_kind": "executable_net",
        "evidence_policy_hash": policy_hash,
        "rolling_folds": fold_results,
        "blocker": (
            None if summary["promotion_ready"]
            else "net_evidence_or_rolling_validation_not_ready"
        ),
        "cost_included": True,
        "cost_basis": "model_ledger_realized_fees_and_execution_rules",
        "benchmark_baseline": "pit_only",
        "benchmark_excess_pct": summary.get("mean_net_excess_pct"),
    })
    summary["evidence_snapshot_id"] = payload_hash(summary)
    return summary


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
    baseline = {
        row["trade_date"]: row
        for row in ledgers.get("pit_only", {}).get("marks", [])
    }
    trial_count = max(1, len(ledgers))
    results = []
    for name, ledger in ledgers.items():
        if not name.startswith("strategy:"):
            continue
        summary = _ledger_summary(
            name=name, ledger=ledger, baseline=baseline, calendar=calendar,
            policy_hash=policy_hash, horizon_days=horizon_days,
            trials_attempted=trial_count,
        )
        summary["strategy_id"] = name.split(":", 1)[1]
        results.append(summary)

    source_outcomes = {}
    for source, ledger_name in {
        "formal": "source:formal",
        "independent": "source:independent",
        "wencai": "source:wencai",
    }.items():
        ledger = ledgers.get(ledger_name)
        if not ledger:
            source_outcomes[source] = {
                "status": "unavailable",
                "reason_code": "forward_source_model_portfolio_missing",
                "effective_samples": 0,
            }
            continue
        summary = _ledger_summary(
            name=ledger_name, ledger=ledger, baseline=baseline, calendar=calendar,
            policy_hash=policy_hash, horizon_days=horizon_days,
            trials_attempted=trial_count,
        )
        summary["status"] = (
            "complete" if summary.get("status") == "sufficient" else "unavailable"
        )
        if summary["status"] == "unavailable":
            summary["reason_code"] = "forward_source_evidence_not_matured"
        source_outcomes[source] = summary

    strata = {name: {} for name in ("industry", "score_bucket", "market_regime")}
    prefixes = {
        "industry": "stratum:industry:",
        "score_bucket": "stratum:score:",
        "market_regime": "stratum:market_regime:",
    }
    for dimension, prefix in prefixes.items():
        for ledger_name, ledger in ledgers.items():
            if not ledger_name.startswith(prefix):
                continue
            label = ledger_name[len(prefix):]
            strata[dimension][label] = _ledger_summary(
                name=ledger_name, ledger=ledger, baseline=baseline,
                calendar=calendar, policy_hash=policy_hash,
                horizon_days=horizon_days, trials_attempted=trial_count,
            )

    return {
        "strategies": results,
        "source_outcomes": source_outcomes,
        "strata": strata,
        "evidence_snapshot_id": payload_hash({
            "strategies": results, "sources": source_outcomes, "strata": strata,
        }),
        "source": "forward_verified_model_accounts",
        "horizon_days": horizon_days,
        "evidence_contract": {
            "data_class": "strict_observed_pit",
            "cost_included": True,
            "benchmark_baseline": "pit_only",
            "independence": "one_mean_per_entry_date_and_nonoverlapping_intervals",
            "historical_backfill_allowed": False,
        },
    }


def scheduled_strategy_evidence(store, *, horizon_days=5, lookback_days=180):
    """Keep price-label diagnostics separate from promotion evidence."""
    diagnostic = store.selection_strategy_evidence(
        horizon_days=horizon_days, lookback_days=lookback_days,
    )
    policy = store.load_active_strategy_policy()
    policy_hash = str((policy or {}).get("policy_hash") or "")
    if policy_hash:
        executable = model_strategy_evidence(
            store, policy_hash, horizon_days=horizon_days,
        )
    else:
        executable = {
            "strategies": [], "source_outcomes": {},
            "strata": {name: {} for name in ("industry", "score_bucket", "market_regime")},
            "source": "forward_verified_model_accounts",
            "horizon_days": horizon_days,
            "evidence_snapshot_id": payload_hash({"reason": "active_policy_missing"}),
            "evidence_contract": {
                "data_class": "strict_observed_pit", "cost_included": True,
                "benchmark_baseline": "pit_only", "historical_backfill_allowed": False,
            },
            "unavailable_reason": "active_policy_missing",
        }
    executable["lookback_days"] = lookback_days
    executable["diagnostic_candidate_outcomes"] = {
        **diagnostic,
        "evidence_kind": "price_label_diagnostic",
        "promotion_eligible": False,
        "reason_code": "not_independent_executable_net_evidence",
    }
    return executable
