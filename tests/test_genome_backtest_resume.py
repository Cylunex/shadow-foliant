from time import monotonic

import pandas as pd

from analysis.genome_bt_worker import portfolio_batch


def test_batch_reuses_only_results_with_matching_frozen_inputs():
    bars = pd.DataFrame(
        {"Open": [10.0, 11.0], "High": [11.0, 12.0], "Low": [9.0, 10.0],
         "Close": [10.5, 11.5], "Volume": [1000, 1100]},
        index=pd.date_range("2026-11-17", periods=2),
    )
    variants = [{"id": 1, "base_strategy": "unknown_fixture", "params": {}}]
    tasks = [(code, "示例", bars, variants, "2026-11-17", 2)
             for code in ("600001", "600002")]
    saved = {}
    run = lambda tasks, seconds: portfolio_batch(
        tasks, saved.get, saved.__setitem__, deadline=monotonic() + seconds,
        cancelled=lambda: False,
    )
    first = run(tasks[:1], 15)
    assert len(first) == 1 and len(saved) == 1
    assert run(tasks, -1) == first
    changed = bars.assign(Close=[10.0, 11.0])
    assert run([("600001", "示例", changed, variants, "2026-11-17", 2)], -1) == []
    assert len(run(tasks, 15)) == len(saved) == 2


def test_worker_excludes_trades_crossing_split_and_pools_actual_wins(monkeypatch):
    import backtest_engine
    from analysis.genome_bt_worker import pooled_returns, run_stock

    trades = [
        {"trigger_date": "2026-09-01", "exit_date": "2026-09-03", "ret_pct": 5, "max_dd_pct": -1},
        {"trigger_date": "2026-09-03", "exit_date": "2026-09-04", "ret_pct": 100, "max_dd_pct": -20},
        {"trigger_date": "2026-09-04", "exit_date": "2026-09-06", "ret_pct": -6, "max_dd_pct": -9},
    ]
    monkeypatch.setattr(backtest_engine, "backtest_one", lambda *args, **kwargs: {"trades": trades})
    variants = [{"id": 1, "base_strategy": "unknown_fixture", "params": {}}]
    _, row = run_stock(("600001", "甲", None, variants, "2026-09-04", 2))[0]
    assert (row["trigger_count"], row["win_rate"], row["best_ret"], row["max_dd"]) == (1, 100, 5, -1)
    assert (row["ho_n"], row["ho_wins"], row["ho_return_sum"]) == (1, 0, -6)
    other = {**row, "trigger_count": 9, "wins": 0, "return_sum": -15}
    assert pooled_returns([row, other]) == (10, -1, 10)


def test_scheduled_retry_reuses_the_saved_cohort_and_variants(monkeypatch):
    from datetime import datetime
    import json

    import automation_config
    import portfolio_db
    import postmarket_digest
    import stock_data
    from analysis import genome_bt_worker, strategy_genome
    from jobs import jobs_hub as hub

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 23, 19, 30, tzinfo=tz)

    holdings = [{'code': '600001', 'name': '甲'}, {'code': '600002', 'name': '乙'}]
    bars = pd.DataFrame({'Close': range(100, 180)})
    availability = {'600001', '600002'}
    snapshots = {}
    outcomes = []
    selections = []

    def variants(strategy, **kwargs):
        selections.append(strategy)
        return [{'id': len(selections), 'base_strategy': strategy, 'params': {'hold_days': 10}}]

    monkeypatch.setattr(hub, 'datetime', Clock)
    monkeypatch.setattr(hub, '_skip_if_not_trading', lambda job: False)
    monkeypatch.setattr(automation_config, 'is_enabled', lambda job: True)
    monkeypatch.setattr(hub, '_latest_job_run_since', lambda job, since: {
        'status': 'partial', 'error': 'resume_pending'
    } if job == 'daily_backtest' else {'status': 'success'})
    monkeypatch.setattr(hub, '_log_run', lambda job, status, **kw: outcomes.append((status, kw['error'])))
    monkeypatch.setattr(hub, '_daily_strategy_scan', lambda: None)
    monkeypatch.setattr(hub, 'get_indicator_snapshot', lambda key, *args: snapshots.get(key))
    monkeypatch.setattr(hub, 'save_indicator_snapshot',
                        lambda key, value: snapshots.update({key: json.loads(json.dumps(value))}))
    monkeypatch.setattr(postmarket_digest, 'add_section', lambda *args, **kw: None)
    monkeypatch.setattr(portfolio_db.portfolio_db, 'get_all_stocks', lambda: holdings)
    monkeypatch.setattr(stock_data.StockDataFetcher, 'get_stock_data',
                        lambda _, code, *a, **kw: bars if code in availability else None)
    monkeypatch.setattr(hub.datahub, 'index_kline', lambda *a: None)
    monkeypatch.setattr(hub.datahub, 'kline_quality', lambda *a, **kw: {'actionable': True})
    for name in ('init_genome_tables', 'seed_default_variants', 'ensure_composed_population'):
        monkeypatch.setattr(strategy_genome, name, lambda **kw: None)
    monkeypatch.setattr(strategy_genome, 'evolution_fitness_pool', lambda pool, **kw: pool)
    monkeypatch.setattr(strategy_genome, 'STRATEGY_PARAM_SPACE', {'enter': {}})
    monkeypatch.setattr(strategy_genome, 'get_variants_for_eval', variants)

    attempts = []
    def batch(tasks, *args, **kwargs):
        attempts.append([(task[0], task[3], task[4], task[5]) for task in tasks])
        return [[('enter:1', {'trigger_count': 0})]]  # one completed stock so far

    monkeypatch.setattr(genome_bt_worker, 'portfolio_batch', batch)
    hub.task_daily_backtest()

    holdings[:] = [{'code': '600003', 'name': '丙'}]
    availability.remove('600002')
    hub.task_daily_backtest(retry_only=True)

    assert selections == ['enter', 'composed']
    assert [task[0] for task in attempts[0]] == ['600001', '600002']
    assert attempts[1] == attempts[0][:1]
    assert outcomes[-1][0] == 'partial'
    assert 'completed=1/2' in outcomes[-1][1] and 'inputs_missing=1' in outcomes[-1][1]
