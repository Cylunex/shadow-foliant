import math
from unittest.mock import patch

import pytest

import _bootstrap  # noqa: F401
import performance
import benchmark


def test_withdrawal_is_not_a_drawdown_and_real_loss_recovers_on_return_path():
    equity = [('2026-01-01', 100), ('2026-01-02', 50), ('2026-01-03', 50)]
    assert performance.risk_metrics(equity, {'2026-01-02': -50})['max_drawdown_pct'] == 0
    equity = [('2026-01-01', 100), ('2026-01-02', 90), ('2026-01-03', 190),
              ('2026-01-04', 190 / .9)]
    risk = performance.risk_metrics(equity, {'2026-01-03': 100})
    assert risk['max_drawdown_pct'] == -10
    assert risk['dd_peak_date'] == '2026-01-01'
    assert risk['dd_trough_date'] == '2026-01-02'
    assert risk['dd_recover_date'] == '2026-01-04'


def test_missing_snapshot_day_includes_intermediate_flow_in_all_metrics():
    from datetime import date, timedelta
    days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(23)]
    equity = [(days[0], 100)] + [(d, 130) for d in days[2:]]
    flows = {days[1]: 30}
    assert performance.twr(equity, flows)['twr_pct'] == 0
    risk = performance.risk_metrics(equity, flows)
    assert risk['volatility_pct'] == risk['max_drawdown_pct'] == 0
    assert risk['sharpe'] is None
    with patch('portfolio_snapshot.get_snapshots', return_value=[
            {'snap_date': d, 'total_mv': v} for d, v in equity]), \
            patch('performance._flows_from_trades', return_value=(flows, [])):
        assert all(v == 0 for _, v in benchmark._portfolio_cum_series())


@pytest.mark.parametrize('equity,flows', [
    ([('2026-01-01', 100), ('2026-01-02', math.nan), ('2026-01-03', 100)], {}),
    ([('2026-01-01', 100), ('2026-01-01', 110)], {}),
    ([('2026-01-02', 100), ('2026-01-01', 110)], {}),
    ([('2026-01-01', 100), ('2026-01-02', 100)], {'2026-01-02': math.inf}),
    ([('2026-01-01', 100), ('2026-01-02', 10), ('2026-01-03', 11)], {}),
])
def test_bad_interval_cannot_be_silently_dropped(equity, flows):
    assert performance.twr(equity, flows) is None
    assert performance.risk_metrics(equity, flows) is None


def test_complete_liquidation_retains_the_final_observation():
    equity = [('2026-01-01', 100), ('2026-01-02', 110), ('2026-01-03', 0)]
    assert performance.twr(equity, {'2026-01-03': -110})['twr_pct'] == 10
    assert performance.risk_metrics(equity, {'2026-01-03': -110})['max_drawdown_pct'] == 0


@pytest.mark.parametrize('flows', [
    [('2026-01-01', -100), ('2026-01-01', 100)],
    [('2026-01-01', -100), ('2027-01-01', math.nan)],
    [('2026-01-01', -100), ('2026-01-01', 100), ('2027-01-01', -50), ('2027-01-01', 50)],
])
def test_xirr_does_not_invent_a_rate_for_degenerate_inputs(flows):
    assert performance.xirr(flows) is None


def test_xirr_and_summary_use_the_same_opening_and_closing_window():
    assert performance.xirr([('2025-01-01', -100), ('2026-01-01', 110)]) == 10
    snaps = [{'snap_date':'2025-01-01', 'total_mv':100}, {'snap_date':'2026-01-01', 'total_mv':110}]
    with patch('portfolio_snapshot.get_snapshots', return_value=snaps), \
            patch.object(performance, '_flows_from_trades', return_value=(
                {'2024-01-01': 1000, '2026-02-01': 1000},
                [('2024-01-01', -1000), ('2026-02-01', -1000)])), \
            patch.object(performance, 'attribution', return_value={}):
        assert performance.summary()['xirr_pct'] == 10


def test_trade_adjustments_are_not_cash_and_read_failure_is_not_zero_flow():
    with patch('portfolio_db.portfolio_db.get_trades', return_value=[
        {'trade_time':'2026-01-01', 'trade_type':'调整', 'amount':'非现金'},
        {'trade_time':'2026-01-01', 'trade_type':'买入', 'amount':20},
        {'trade_time':'2026-01-02', 'trade_type':'卖出', 'amount':10},
    ]):
        assert performance._flows_from_trades() == (
            {'2026-01-01':20, '2026-01-02':-10}, [('2026-01-01',-20), ('2026-01-02',10)])
    with patch('portfolio_db.portfolio_db.get_trades', side_effect=RuntimeError('offline')), \
            patch('portfolio_snapshot.get_snapshots', return_value=[
                {'snap_date':'2026-01-01', 'total_mv':100}, {'snap_date':'2026-01-02', 'total_mv':110}]), \
            patch.object(performance, 'attribution', return_value={}):
        report = performance.summary()
        assert report['twr'] is report['risk'] is report['xirr_pct'] is None
        assert report['warnings']


def test_missing_amount_outside_snapshot_window_does_not_poison_window():
    rows = [
        {'trade_time':'2025-12-01', 'trade_type':'卖出', 'amount':None},
        {'trade_time':'2026-01-01', 'trade_type':'买入', 'amount':None},
        {'trade_time':'2026-01-02', 'trade_type':'买入', 'amount':10},
        {'trade_time':'2026-02-01', 'trade_type':'卖出', 'amount':None},
    ]
    with patch('portfolio_db.portfolio_db.get_trades', return_value=rows):
        assert performance._flows_from_trades('2026-01-01', '2026-01-31') == (
            {'2026-01-02':10}, [('2026-01-02',-10)])
        assert performance._flows_from_trades('2025-11-01', '2026-01-31') == (None, None)
    snaps = [{'snap_date':'2026-01-01', 'total_mv':100}, {'snap_date':'2026-01-03', 'total_mv':110}]
    with patch('portfolio_db.portfolio_db.get_trades', return_value=rows), \
            patch('portfolio_snapshot.get_snapshots', return_value=snaps), \
            patch.object(performance, 'attribution', return_value={}):
        assert performance.summary()['twr']['twr_pct'] == 0
        assert benchmark._portfolio_cum_series() == [('2026-01-01', 0), ('2026-01-03', 0)]


def test_stale_or_missing_benchmark_endpoint_cannot_report_excess():
    with patch.object(benchmark, '_portfolio_cum_series', return_value=[
            ('2026-01-01', 0), ('2026-01-03', 10)]):
        for quotes in ({'2026-01-01':100, '2026-01-02':110},
                       {'2026-01-02':100, '2026-01-03':110},
                       {'2026-01-01':100, '2026-01-03':math.nan}):
            with patch.object(benchmark, '_index_close_map', return_value=quotes):
                result = benchmark.compare()
                assert result['excess_pct'] is None
                assert result['comparison_status'] == 'unmatched_endpoint_dates'
        with patch.object(benchmark, '_index_close_map', return_value={'2026-01-01':100, '2026-01-03':105}):
            assert benchmark.compare()['excess_pct'] == 5
