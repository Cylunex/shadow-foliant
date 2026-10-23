"""Read-only cohort statistics for stored price labels, never trading returns."""
from collections import defaultdict
from datetime import date
from math import isfinite
from statistics import mean


def summarize_selection_performance(rows, *, start_date, end_date, as_of, horizon_days):
    start, end, cutoff = (date.fromisoformat(v).isoformat()
                          for v in (start_date, end_date, as_of))
    if start > end or end > cutoff or type(horizon_days) is not int or horizon_days < 1:
        raise ValueError("invalid_performance_window")
    cohorts = defaultdict(dict)
    for row in rows:
        if not start <= row['selection_date'] <= end:
            continue
        key = (row['selection_date'], row['selection_run_id'], row['strategy_id'],
               row['strategy_version'], row['lane'])
        identity = row['nomination_id']
        if identity in cohorts[key]:
            raise ValueError("duplicate_nomination_in_performance_window")
        cohorts[key][identity] = row
    daily = []
    for key, nominations in sorted(cohorts.items()):
        valid, versions, intervals = [], set(), set()
        invalid = awaiting = 0
        for row in nominations.values():
            entry, exit_date = row.get('entry_date'), row.get('exit_date')
            if (row.get('outcome_status') != 'matured'
                    or row.get('horizon_days') != horizon_days
                    or (exit_date and exit_date > cutoff)):
                awaiting += 1
                continue
            value = row.get('return_pct')
            if (not entry or not exit_date or not key[0] < entry <= exit_date <= cutoff
                    or isinstance(value, bool) or not isinstance(value, (float, int))
                    or not isfinite(value) or not row.get('metric_version')):
                invalid += 1
                continue
            valid.append(value)
            versions.add(row['metric_version'])
            intervals.add((entry, exit_date))
        # A cohort is comparable only when every nomination shares the same
        # observation interval and metric version. Missing labels are not losses.
        complete = len(valid) == len(nominations) and len(versions) == len(intervals) == 1
        daily.append(dict(selection_date=key[0], selection_run_id=key[1],
                          strategy_id=key[2], strategy_version=key[3], lane=key[4],
                          nomination_count=len(nominations), matured_count=len(valid),
                          positive_count=sum(v > 0 for v in valid),
                          awaiting_outcome_count=awaiting, invalid_count=invalid,
                          coverage_pct=round(100 * len(valid) / len(nominations), 2),
                          status='complete' if complete else 'incomplete',
                          metric_versions=sorted(versions), intervals=sorted(intervals),
                          avg_return_pct=round(mean(valid), 6) if complete else None,
                          win_rate_pct=round(100 * sum(v > 0 for v in valid) / len(valid), 2)
                          if complete else None))
    buckets = defaultdict(list)
    for cohort in daily:
        buckets[(cohort['strategy_id'], cohort['strategy_version'], cohort['lane'])].append(cohort)
    strategies = []
    for key, items in sorted(buckets.items()):
        complete = [r for r in items if r['status'] == 'complete']
        versions = {r['metric_versions'][0] for r in complete}
        comparable = bool(complete) and len(versions) == 1
        strategies.append(dict(strategy_id=key[0], strategy_version=key[1], lane=key[2],
            cohort_count=len(items), complete_cohort_count=len(complete),
            nomination_count=sum(r['nomination_count'] for r in items),
            matured_count=sum(r['matured_count'] for r in items),
            mean_cohort_return_pct=round(mean(r['avg_return_pct'] for r in complete), 6)
            if comparable else None,
            mean_cohort_win_rate_pct=round(mean(100 * r['positive_count'] / r['matured_count']
                                               for r in complete), 2)
            if comparable else None))
    pairs = []
    runs = defaultdict(dict)
    for cohort in daily:
        runs[(cohort['selection_run_id'], cohort['strategy_version'])][cohort['strategy_id']] = cohort
    for (run_id, _), items in sorted(runs.items()):
        top5 = items.get('formal_local_fusion_top5')
        top15 = items.get('formal_local_fusion_top15')
        if (top5 and top15 and top5['status'] == top15['status'] == 'complete'
                and top5['metric_versions'] == top15['metric_versions']
                and top5['intervals'] == top15['intervals']
                and top5['strategy_version'] == top15['strategy_version']):
            pairs.append(dict(selection_run_id=run_id, selection_date=top5['selection_date'],
                top5_minus_top15_pct=round(top5['avg_return_pct'] - top15['avg_return_pct'], 6)))
    return dict(start_date=start, end_date=end, as_of=cutoff, horizon_days=horizon_days,
                return_kind='signal_price_observation', executable_round_trip=False,
                costs_included=False, benchmark_status='not_evaluated',
                aggregation='equal_weight_complete_cohorts',
                limitations=['awaiting_outcome_may_mean_immature_or_missing_data',
                             'current_stored_labels_not_historical_as_known_snapshot',
                             'overlapping_cohorts_are_not_independent_samples'],
                strategies=strategies, daily=daily, top5_vs_top15=pairs)
