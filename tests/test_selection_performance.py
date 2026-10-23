import sqlite3

import pytest

from analysis.selection_performance import summarize_selection_performance as summarize
from data.research_store import ResearchStore
from scripts.selection_performance import read_performance


def row(identity='a', **overrides):
    return dict(nomination_id=identity, selection_run_id='run', symbol=identity,
                strategy_id='formal_local_fusion_top15', strategy_version='v2', lane='formal',
                selection_date='2026-10-20', horizon_days=1, outcome_status='matured',
                entry_date='2026-10-21', exit_date='2026-10-21', return_pct=2.,
                metric_version='signal-price-v2') | overrides


def report(rows, **kwargs):
    return summarize(rows, **({'start_date': '2026-10-17', 'end_date': '2026-10-23',
                              'as_of': '2026-10-23', 'horizon_days': 1} | kwargs))


def test_pending_labels_remain_in_denominator_and_suppress_partial_average():
    result = report([row(), row('b', outcome_status=None, return_pct=None)])
    cohort = result['daily'][0]
    assert cohort['nomination_count'] == 2
    assert cohort['coverage_pct'] == 50
    assert cohort['awaiting_outcome_count'] == 1
    assert cohort['avg_return_pct'] is None
    assert result['strategies'][0]['mean_cohort_return_pct'] is None


def test_complete_cohorts_compare_same_run_and_equal_weight_dates():
    rows = [row(), row('b', return_pct=-1),
            row('c', strategy_id='formal_local_fusion_top5', return_pct=-2),
            row('d', selection_run_id='next', selection_date='2026-10-21',
                entry_date='2026-10-22', exit_date='2026-10-22', return_pct=3)]
    result = report(rows)
    assert result['top5_vs_top15'][0]['top5_minus_top15_pct'] == -2.5
    assert result['strategies'][0]['mean_cohort_return_pct'] == 1.75
    assert result['costs_included'] is result['executable_round_trip'] is False
    assert result['benchmark_status'] == 'not_evaluated'


@pytest.mark.parametrize('override', [
    {'return_pct': float('nan')}, {'entry_date': '2026-10-20'},
    {'metric_version': None}, {'return_pct': float('inf')}])
def test_invalid_labels_are_visible_and_excluded(override):
    result = report([row(**override)])['daily'][0]
    assert result['invalid_count'] == 1
    assert result['avg_return_pct'] is None


def test_future_horizon_and_mixed_metric_or_interval_cannot_create_comparison():
    assert report([row(exit_date='2026-10-26')])['daily'][0]['awaiting_outcome_count'] == 1
    assert report([row(horizon_days=5)])['daily'][0]['awaiting_outcome_count'] == 1
    assert report([row(), row('b', metric_version='legacy-price-v1')])['daily'][0]['status'] == 'incomplete'
    rows = [row(), row('b', strategy_id='formal_local_fusion_top5',
                       entry_date='2026-10-22', exit_date='2026-10-22')]
    assert report(rows)['top5_vs_top15'] == []
    with pytest.raises(ValueError, match='duplicate_nomination'):
        report([row(), row()])


def test_read_performance_keeps_unmatured_nominations_and_executes_only_select(tmp_path):
    path = str(tmp_path / 'research.db')
    store = ResearchStore(path, connect_fn=sqlite3.connect)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO selection_strategy_runs VALUES ('s','run','strategy','v1','core',"
                 "'success','2026-10-20','','v1','{}','2026-10-20')")
    conn.execute("INSERT INTO selection_candidate_nominations VALUES ('n','run','s','600001',"
                 "'core','strategy','v1',1,1,1,'eligible','{}','2026-10-20')")
    conn.commit()
    conn.close()
    statements = []
    def readonly(_):
        connection = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
        connection.set_trace_callback(statements.append)
        return connection
    store = ResearchStore(path, connect_fn=readonly, ensure_schema=False)
    result = read_performance(store, start_date='2026-10-17', end_date='2026-10-23',
                              as_of='2026-10-23')
    assert result['daily'][0]['nomination_count'] == 1
    assert result['daily'][0]['matured_count'] == 0
    assert all(s.lstrip().startswith('SELECT') for s in statements)
