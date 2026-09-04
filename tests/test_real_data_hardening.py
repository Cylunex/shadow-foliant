"""Regression contracts derived from real NAS acceptance failures."""
from contextlib import nullcontext
from datetime import datetime
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from data.market_history_freeze import audit_history, freeze_batch
from data.research_store import ResearchStore


@pytest.fixture
def store(tmp_path):
    return ResearchStore(str(tmp_path / 'research.db'), connect_fn=sqlite3.connect)


def seed_legacy(store):
    frame = pd.DataFrame([{'symbol': '600001', 'trade_date': day, 'close': 12.5,
                           'volume': 1000, 'amount': 12500, 'is_st': False}
                          for day in ['2026-09-01', '2026-09-02', '2026-09-03']])
    frame.attrs['provenance'] = {'provider': 'fixture', 'retrieved_at': '2026-09-03T18:30:00+08:00'}
    store.upsert_daily_bars(frame)
    with store.connect() as conn:
        original = conn.execute('SELECT dataset_id FROM research_daily_bars LIMIT 1').fetchone()[0]
        conn.execute("DELETE FROM research_market_observations WHERE trade_date<'2026-09-03'")
    return original


def test_freeze_is_bounded_resumable_and_never_rewrites_prices_or_old_observations(store):
    original = seed_legacy(store)
    with store.connect() as conn:
        before = conn.execute('SELECT close,volume,amount,retrieved_at FROM research_daily_bars ORDER BY trade_date').fetchall()
    publication = store.generation_vector(['daily_market'])
    assert audit_history(store, through='2026-09-03')['unfrozen_rows'] == 2
    assert freeze_batch(store, through='2026-09-03', limit=1)['frozen_rows'] == 1
    assert audit_history(store, through='2026-09-03')['unfrozen_rows'] == 1
    assert freeze_batch(store, through='2026-09-03', limit=1)['frozen_rows'] == 1
    assert freeze_batch(store, through='2026-09-03')['frozen_rows'] == 0
    assert store.generation_vector(['daily_market']) == publication
    assert store.generation_vector(['market_archive']) == {'market_archive': 2}
    with store.connect() as conn:
        assert before == conn.execute('SELECT close,volume,amount,retrieved_at FROM research_daily_bars ORDER BY trade_date').fetchall()
        assert conn.execute('SELECT COUNT(*) FROM research_market_observations WHERE dataset_id=?', (original,)).fetchone()[0] == 1
        payload = json.loads(conn.execute("SELECT payload FROM research_market_observations WHERE dataset_id<>? LIMIT 1", (original,)).fetchone()[0])
        assert payload['archive_class'] == 'legacy_capture_not_historical_pit'
        assert payload['retrieved_at'] == '2026-09-03T18:30:00+08:00'


def test_freeze_failure_rolls_back_observations_and_bar_pointers(store):
    original = seed_legacy(store)
    with patch.object(store, '_advance_generation', side_effect=RuntimeError('fault')):
        with pytest.raises(RuntimeError):
            freeze_batch(store, through='2026-09-03')
    assert audit_history(store, through='2026-09-03')['unfrozen_rows'] == 2
    with store.connect() as conn:
        assert conn.execute('SELECT DISTINCT dataset_id FROM research_daily_bars').fetchall() == [(original,)]


def test_replay_never_substitutes_new_legacy_capture_into_old_manifest(store):
    original = seed_legacy(store)
    manifest = {'market_dataset_ids': [original], 'decision_context': {'market_cutoff': '2026-09-03',
                'market_cutoff_inclusive': True}, 'policy': {'min_history_days': 70}}
    with patch.object(store, 'load_selection_manifest', return_value=manifest):
        with patch.object(store, 'load_daily_panel_from_manifest', side_effect=AssertionError('aggregate only')):
            before = store.manifest_market_coverage('old')
        freeze_batch(store, through='2026-09-03')
        assert store.manifest_market_coverage('old') == before
        assert before['max_history_days'] == 1
        assert before['symbols_with_required_history'] == 0
        assert before['live_history_fallback'] is False
        manifest['decision_context']['market_cutoff'] = '2026-09-02'
        assert store.load_daily_panel_from_manifest('old').empty


@pytest.mark.parametrize('limit', [0, 10001])
def test_freeze_rejects_unbounded_batches(store, limit):
    with pytest.raises(ValueError):
        freeze_batch(store, through='2026-09-03', limit=limit)


@pytest.mark.parametrize('hour,minute,mode', [(16, 1, 'preopen'), (18, 4, 'preopen'), (18, 5, 'postclose')])
def test_data_mode_follows_upstream_publication_window(hour, minute, mode):
    import core.research_health as health
    clock = datetime(2026, 9, 4, hour, minute, tzinfo=health.A_SHARE_TIMEZONE)
    with patch.object(health, 'datetime') as mocked:
        mocked.now.return_value = clock
        assert health._default_mode('2026-09-04') == mode


def test_formal_readiness_does_not_switch_to_postclose():
    import core.research_health as health
    with patch.object(health, 'cached_snapshot', return_value={}) as read:
        health.selection_snapshot(selection_date='2026-09-04')
        assert read.call_args.kwargs['mode'] == 'preopen'


def test_data_pending_is_separate_from_usable_morning_inputs():
    import core.research_health as health
    with patch.object(health, 'datetime') as clock, patch.object(health, 'cached_snapshot', return_value={'ready': True}):
        clock.now.return_value = datetime(2026, 9, 4, 16, tzinfo=health.A_SHARE_TIMEZONE)
        report = health.data_snapshot(selection_date='2026-09-04')
        assert report['ready']
        assert report['postclose_acquisition']['ready'] is False
        assert '18:05:00' in report['postclose_acquisition']['scheduled_sync_at']


def test_evening_refresh_warms_both_contexts():
    with patch('core.research_health._default_mode', return_value='postclose'), patch(
            'core.research_health.refresh_quality_report', return_value={'status': 'ready'}) as refresh:
        from jobs.decision_loop_jobs import refresh_quality
        refresh_quality()
        assert refresh.call_count == 2
        assert refresh.call_args.kwargs['mode'] == 'preopen'


@pytest.fixture
def wencai(monkeypatch):
    from data.sources import pywencai as source
    for name, value in [('_rejected_until', 0), ('_rejected_status', None), ('_inflight', None),
                        ('_streak_fail', 0), ('_last_fail', 0)]:
        monkeypatch.setattr(source, name, value)
    monkeypatch.setattr('data.provider_governor.provider_slot', lambda *_a, **_kw: nullcontext())
    return source


@pytest.mark.parametrize('status,minimum', [(401, 3590), (403, 890), (429, 119), (503, 59)])
def test_http_rejection_blocks_inner_and_outer_retries(wencai, status, minimum):
    request = Mock(return_value=SimpleNamespace(status_code=status, headers={}))
    proxy = wencai._HttpsRequestsProxy(SimpleNamespace(request=request))
    with pytest.raises(wencai.PyWencaiRequestRejected) as error:
        proxy.request('GET', 'https://www.iwencai.com/test')
    assert error.value.status_code == status
    assert wencai.rejection_status()['retry_after_seconds'] >= minimum
    with pytest.raises(wencai.PyWencaiRequestRejected):
        proxy.request('GET', 'https://www.iwencai.com/test')
    with pytest.raises(wencai.PyWencaiRequestRejected):
        wencai.pywencai_get('test')
    assert request.call_count == 1


def test_retry_after_is_bounded_and_cooldown_can_recover(wencai, monkeypatch):
    request = Mock(return_value=SimpleNamespace(status_code=429, headers={'Retry-After': '99999'}))
    proxy = wencai._HttpsRequestsProxy(SimpleNamespace(request=request))
    with pytest.raises(wencai.PyWencaiRequestRejected):
        proxy.request('GET', 'https://www.iwencai.com/test')
    assert wencai.rejection_status()['retry_after_seconds'] <= 3600
    monkeypatch.setattr(wencai, '_rejected_until', 0)
    request.return_value.status_code = 200
    assert proxy.request('GET', 'https://www.iwencai.com/test').status_code == 200


def test_timeout_does_not_queue_another_wencai_worker(wencai, monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    def slow(*_a, **_kw):
        entered.set()
        release.wait(2)
        return None
    monkeypatch.setattr(wencai, '_invoke_pywencai', slow)
    try:
        with pytest.raises(TimeoutError):
            wencai.pywencai_get('first', timeout=.02)
        assert entered.is_set()
        original = wencai._inflight
        with pytest.raises(TimeoutError, match='仍未结束'):
            wencai.pywencai_get('second', timeout=.02)
        assert wencai._inflight is original
    finally:
        release.set()
        wencai._inflight.result(timeout=2)


def test_holdout_missing_docker_is_rejected_before_reading_facts(tmp_path):
    from application.research_lab import isolated_holdout_evaluator
    facts = tmp_path / 'sealed.json'
    facts.touch(mode=0o600)
    with patch('application.research_lab.shutil.which', return_value=None), patch(
            'pathlib.Path.read_bytes', side_effect=AssertionError('must stay sealed')):
        with pytest.raises(RuntimeError, match='before_reservation'):
            isolated_holdout_evaluator(facts, image='research@sha256:' + 'a' * 64, expected_digest='b' * 64)


def test_holdout_preflight_uses_only_public_synthetic_facts(tmp_path):
    from application.research_lab import isolated_holdout_evaluator
    facts = tmp_path / 'sealed.json'
    facts.touch(mode=0o600)
    with patch('application.research_lab.shutil.which', return_value='/usr/bin/docker'), patch(
            'pathlib.Path.read_bytes', side_effect=AssertionError('must stay sealed')), patch(
            'application.research_lab.run_isolated', return_value={'values': [1.0]}) as run:
        evaluator = isolated_holdout_evaluator(facts, image='research@sha256:' + 'a' * 64, expected_digest='b' * 64)
        assert callable(evaluator)
        assert run.call_args.args[1] == {'probe': [1.0]}


def test_holdout_broken_worker_rejected_in_preflight(tmp_path):
    from application.research_lab import isolated_holdout_evaluator
    facts = tmp_path / 'sealed.json'
    facts.touch(mode=0o600)
    with patch('application.research_lab.shutil.which', return_value='/usr/bin/docker'), patch(
            'application.research_lab.run_isolated', return_value={'values': [2]}):
        with pytest.raises(RuntimeError, match='preflight_failed'):
            isolated_holdout_evaluator(facts, image='research@sha256:' + 'a' * 64, expected_digest='b' * 64)
