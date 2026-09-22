from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from jobs.closing_trade_plans import build_closing_plans, _cached_bars
from application.scheduled_snapshot import _plan_rebuild_evidence, _job_run

NOW = datetime(2026, 9, 22, 20, 20, tzinfo=ZoneInfo('Asia/Shanghai'))


def formal():
    return {'run_id': 'formal-test', 'selection_date': '2026-09-22', 'artifacts': {
        'formal_top15': {'payload': [{'symbol': '600001', 'rank': 1}]},
        'formal_top5': {'payload': [{'symbol': '600001', 'rank': 1}]},
    }}


def bars(day='2026-09-22'):
    frame = pd.DataFrame({'Open': 10., 'High': 11., 'Low': 9., 'Close': 10., 'Volume': 100.},
                         index=pd.bdate_range(end=day, periods=80))
    frame.attrs['datahub_cache_written_at'] = NOW.replace(hour=18).timestamp()
    return frame


def plan_builder(_symbol, _frame, **_kwargs):
    return {'available': True, 'action': 'hold', 'entry_low': 9.9, 'entry_high': 10.,
            'stop_loss': 9., 'target_price': 11.}


def test_close_rebuild_covers_holdings_and_formal_without_reusing_morning_input():
    calls = []
    def load(symbol):
        calls.append(symbol)
        return bars()
    result = build_closing_plans(formal=formal(), holdings=[{'code': '510300', 'quantity': 100}],
                                now=NOW, bar_loader=load, plan_builder=plan_builder)
    assert result['status'] == 'complete'
    assert set(calls) == {'510300', '600001'}
    assert result['available_count'] == result['requested_count'] == 2
    assert result['selection_run_id'] == 'formal-test'
    for plan in result['plans'].values():
        assert _plan_rebuild_evidence(plan, '2026-09-22')['status'] == 'current_close_rebuild'
        assert len(plan['input_hash']) == 64
        assert plan['auto_execution'] is False


def test_real_rule_builder_operates_on_local_bars():
    result = build_closing_plans(formal=formal(), holdings=[], now=NOW,
                                bar_loader=lambda _: bars())
    plan = result['plans']['600001']
    assert plan['available'] is True
    assert plan['current_price'] == 10.
    assert plan['market_action'] == 'unknown'


@pytest.mark.parametrize('day', ['2026-09-21', '2026-09-23'])
def test_prior_or_future_daily_bars_do_not_generate_prices(day):
    result = build_closing_plans(formal=formal(), holdings=[], now=NOW,
                                bar_loader=lambda _: bars(day), plan_builder=plan_builder)
    assert result['status'] == 'degraded'
    plan = result['plans']['600001']
    assert plan['available'] is False
    assert plan['blockers'] == ['closing_daily_bars_not_current']
    assert 'stop_loss' not in plan


def test_one_failed_symbol_does_not_discard_other_plans():
    def load(symbol):
        if symbol == '510300':
            raise RuntimeError('private backend message')
        return bars()
    result = build_closing_plans(formal=formal(), holdings=[{'code': '510300', 'quantity': 100}],
                                now=NOW, bar_loader=load, plan_builder=plan_builder)
    assert result['status'] == 'degraded'
    assert result['available_count'] == 1
    assert result['plans']['510300']['blockers'] == ['closing_plan_build_failed']
    assert 'private' not in str(result)


def test_budget_expiry_records_gaps_without_loading_bars():
    result = build_closing_plans(formal=formal(), holdings=[], now=NOW, budget_seconds=0,
                                bar_loader=lambda _: pytest.fail('budget exhausted'))
    assert result['plans']['600001']['blockers'] == ['closing_plan_budget_exhausted']


def test_wrong_selection_and_intraday_do_not_rebuild():
    for value, now in [(dict(formal(), selection_date='2026-09-21'), NOW),
                       (formal(), NOW.replace(hour=14))]:
        result = build_closing_plans(formal=value, holdings=[], now=now,
                                    bar_loader=lambda _: pytest.fail('unexpected read'))
        assert result['status'] == 'skipped'


def test_cache_loader_never_starts_market_requests(monkeypatch):
    import datahub
    calls = []
    monkeypatch.setattr(datahub, 'kline', lambda *args, **kwargs: calls.append((args, kwargs)))
    _cached_bars('600001')
    assert calls == [(('600001', '1y', '1d'), {'adjust': 'qfq', 'cache_only': True})]


@pytest.mark.parametrize('hour,expected', [(14, False), (18, True), (21, False)])
def test_disk_cache_time_proves_post_close_input(tmp_path, monkeypatch, hour, expected):
    import os
    import datahub
    monkeypatch.setattr(datahub, '_KLINE_DIR', str(tmp_path))
    monkeypatch.setattr(datahub, '_route', lambda *a, **k: pytest.fail('unexpected provider call'))
    # Deliberately preserve a misleading embedded timestamp; the actual file wins.
    cache = tmp_path / '600001_1y_1d_qfq.pkl'
    bars().to_pickle(cache)
    timestamp = NOW.replace(hour=hour, minute=59 if hour == 14 else 0).timestamp()
    os.utime(cache, (timestamp, timestamp))
    frame = _cached_bars('600001')
    assert frame.attrs['datahub_cache_written_at'] == timestamp
    result = build_closing_plans(formal=formal(), holdings=[], now=NOW,
                                bar_loader=lambda _: frame, plan_builder=plan_builder)
    plan = result['plans']['600001']
    assert plan['available'] is expected
    if not expected:
        assert plan['blockers'] == ['closing_daily_cache_not_post_close']


def test_missing_cache_timestamp_cannot_prove_closed_bar():
    frame = bars()
    frame.attrs.clear()
    result = build_closing_plans(formal=formal(), holdings=[], now=NOW,
                                bar_loader=lambda _: frame, plan_builder=plan_builder)
    assert result['plans']['600001']['blockers'] == ['closing_daily_cache_time_missing']


def test_snapshot_refresh_time_is_not_plan_generation_evidence():
    plan = {'plan_as_of': '2026-09-22', '_snapshot_generated_at': NOW.isoformat()}
    assert _plan_rebuild_evidence(plan, '2026-09-22')['status'] == 'historical_reference'
    plan['plan_generated_at'] = NOW.astimezone(timezone.utc).isoformat()
    assert _plan_rebuild_evidence(plan, '2026-09-22')['blockers'] == [
        'trade_plan_input_provenance_missing']
    plan.update(price_basis='post_close_cached_qfq',
                input_cached_at=NOW.replace(hour=18).isoformat(), input_hash='a' * 64)
    assert _plan_rebuild_evidence(plan, '2026-09-22')['status'] == 'current_close_rebuild'


@pytest.mark.parametrize('cached_at', [
    '2026-09-22T14:59:00+08:00',
    '2026-09-21T18:00:00+08:00',
    '2026-09-22T20:21:00+08:00',
])
def test_post_close_plan_rejects_unproven_cache_time(cached_at):
    plan = {'available': True, 'plan_as_of': '2026-09-22',
            'plan_generated_at': NOW.isoformat(),
            'price_basis': 'post_close_cached_qfq',
            'input_cached_at': cached_at, 'input_hash': 'a' * 64}
    assert _plan_rebuild_evidence(plan, '2026-09-22')['blockers'] == [
        'trade_plan_input_provenance_missing']


@pytest.mark.parametrize('detail,code', [
    ('external_err=RuntimeError', 'external_research_outcome_failed'),
    ('decision_loop=partial', 'decision_loop_incomplete'),
])
def test_partial_outcomes_are_visible_even_when_job_wrapper_succeeded(detail, code):
    result = _job_run({'job_name': 'eod_outcomes', 'status': 'success', 'error': detail})
    assert result['status'] == 'degraded'
    assert code in result['partial_failure_codes']


def test_closing_job_registered_and_persisted_without_notifications(monkeypatch):
    from jobs import jobs_hub as hub
    from jobs import closing_trade_plans as closing
    from jobs.automation_config import REGISTRY
    from jobs.schedule_policy import MARKET_DATA_TIMES
    assert MARKET_DATA_TIMES['closing_trade_plans'] == '20:20'
    assert REGISTRY['closing_trade_plans']['depends_on'] == ['kline_prefetch']
    events = []
    monkeypatch.setattr(hub, '_skip_if_not_trading', lambda _: False)
    monkeypatch.setattr(closing, 'refresh_closing_plans', lambda: {'status': 'complete',
                        'requested_count': 2, 'available_count': 2})
    monkeypatch.setattr(hub, '_log_run', lambda *args, **kwargs: events.append((args, kwargs)))
    hub.task_closing_trade_plans()
    assert events[0][0] == ('closing_trade_plans', 'success')
    assert events[0][1]['notify'] is False


def test_only_current_formal_candidates_join_prefetch_pool():
    from jobs.jobs_hub import _today_formal_prefetch_symbols
    assert _today_formal_prefetch_symbols(formal(), '2026-09-22') == ['600001']
    assert _today_formal_prefetch_symbols(formal(), '2026-09-23') == []
