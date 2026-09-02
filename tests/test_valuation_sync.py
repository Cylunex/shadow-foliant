from contextlib import nullcontext
from datetime import datetime
import sqlite3
from unittest.mock import Mock

import pandas as pd
import pytest

from data.research_store import ResearchStore
from data.valuation_contract import TZ, closing_timestamp, complete, iso_day, merge_snapshot
from data.valuation_sync import ValuationSynchronizer
from data.sources import valuation, baostock, tencent, eastmoney

DAY = '2026-09-01'


def frame(code='000001', **fields):
    return pd.DataFrame([{'symbol': code, 'trade_date': DAY, **fields}])


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('VALUATION_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('VALUATION_EVENING_BUDGET_SECONDS', '120')
    monkeypatch.setenv('VALUATION_PREMARKET_BUDGET_SECONDS', '120')
    monkeypatch.setattr('data.valuation_sync.sources.historical', lambda *_: pd.DataFrame())
    monkeypatch.setattr('data.valuation_sync.sources.live', lambda *_: pd.DataFrame())
    monkeypatch.setattr('data.valuation_sync.baostock.valuation_day', lambda *_: pd.DataFrame())
    return ResearchStore(str(tmp_path / 'research.db'), connect_fn=sqlite3.connect)


def test_same_day_priority_field_merge_and_negative_pe():
    rows = merge_snapshot([], frame(pe_ttm=10, pb=2), day=DAY, provider='zzshare')
    result = merge_snapshot(rows, frame(pe_ttm=99, pb=9, market_cap=100), day=DAY, provider='tencent')
    assert result[0]['pe_ttm'] == 10 and result[0]['market_cap'] == 100
    assert result[0]['field_sources']['pb']['provider'] == 'zzshare'
    assert 'market_cap' not in rows[0]  # no mutation of caller/checkpoint
    assert complete({'pe_ttm': -4, 'pb': -.5})
    assert not complete({'pb': 1})
    wrong = frame(pe_ttm=1, pb=1)
    wrong['trade_date'] = '2026-09-02'
    assert merge_snapshot(result, wrong, day=DAY, provider='zzshare') == result


@pytest.mark.parametrize('value', ['bad', 'NaT', '', None, '20260831150100', '20260901145959'])
def test_live_timestamp_rejects_unknown_wrong_day_and_intraday(value):
    assert not closing_timestamp(value, DAY)


def test_numeric_date_and_shanghai_timestamp():
    assert iso_day(20260901) == DAY
    assert closing_timestamp('20260901150001', DAY).startswith(DAY)
    assert closing_timestamp('2026-09-01T07:01:00Z', DAY).startswith(DAY)


def test_stable_success_does_not_fan_out(store, monkeypatch):
    calls = []
    def fetch(provider, day):
        calls.append(provider)
        return frame(pe_ratio=11, pb_ratio=2, market_cap=100, pe_ratio_lyr=12)
    monkeypatch.setattr('data.valuation_sync.sources.historical', fetch)
    result = ValuationSynchronizer(store).sync(DAY, ['000001'])
    assert calls == ['zzshare'] and result['valuation_rows'] == 1
    saved = store.load_valuations(DAY).iloc[0]
    assert saved['market_cap'] == 100 and saved['pe_lyr'] == 12


def test_failed_primary_uses_bao_and_keeps_error_sanitized(store, monkeypatch):
    def fetch(provider, day):
        raise RuntimeError('https://secret-token.example.com/private')
    monkeypatch.setattr('data.valuation_sync.sources.historical', fetch)
    monkeypatch.setattr('data.valuation_sync.baostock.valuation_day', lambda *_: frame(pe_ttm=10, pb=2))
    result = ValuationSynchronizer(store).sync(DAY, ['000001'])
    assert result['valuation_rows'] == 1 and 'secret-token' not in str(result)
    assert store.load_valuation_evidence(DAY)[0]['field_sources']['pe_ttm']['provider'] == 'baostock'


def test_resume_only_missing_and_preserve_partial_with_exact_replay(store, monkeypatch):
    monkeypatch.setenv('VALUATION_BAOSTOCK_SYMBOLS_PER_RUN', '1')
    monkeypatch.setattr('data.valuation_sync.sources.historical',
                        lambda *_: frame(pb_ratio=2, market_cap=100))
    requested = []
    def fetch(code, day):
        requested.append(code)
        return frame(code, pe_ttm=20, pb=3)
    monkeypatch.setattr('data.valuation_sync.baostock.valuation_day', fetch)
    syncer = ValuationSynchronizer(store)
    first = syncer.sync(DAY, ['000001', '000002'])
    assert first['valuation_rows'] == 1
    before = store.load_valuations(DAY, exact=True)
    old_ids = before.dataset_id.unique().tolist()
    second = syncer.sync(DAY, ['000001', '000002'])
    assert requested == ['000001', '000002'] and second['valuation_rows'] == 2
    live = store.load_valuations(DAY, exact=True)
    assert live.dataset_id.nunique() == 1
    assert live.set_index('symbol').loc['000001', 'pb'] == 2  # primary kept
    monkeypatch.setattr(store, 'load_selection_manifest', lambda _: {'valuation_dataset_ids': old_ids})
    old_replay = store.load_valuations_from_manifest('old')
    pd.testing.assert_frame_equal(before.sort_index(axis=1), old_replay.sort_index(axis=1),
                                  check_dtype=False)


def test_pb_only_snapshot_is_not_counted_as_usable(store, monkeypatch):
    monkeypatch.setenv('VALUATION_BAOSTOCK_SYMBOLS_PER_RUN', '0')
    monkeypatch.setattr('data.valuation_sync.sources.historical', lambda *_: frame(pb_ratio=2))
    result = ValuationSynchronizer(store).sync(DAY, ['000001'])
    assert result['valuation_rows'] == 0 and result['valuation_stored_rows'] == 1
    assert result['valuation_field_coverage']['pb'] == 1
    saved = store.load_valuations(DAY)
    assert saved.iloc[0].quality_status == 'partial'
    monkeypatch.setattr(store, 'load_selection_manifest',
                        lambda _: {'valuation_dataset_ids': saved.dataset_id.tolist()})
    assert store.load_valuations_from_manifest('test').iloc[0].quality_status == 'partial'


def test_bulk_bootstrap_does_not_do_single_stock_or_live_requests(store, monkeypatch):
    single = Mock(side_effect=AssertionError('must not request per-symbol'))
    live = Mock(side_effect=AssertionError('must not use live data for bootstrap'))
    monkeypatch.setattr('data.valuation_sync.baostock.valuation_day', single)
    monkeypatch.setattr('data.valuation_sync.sources.live', live)
    ValuationSynchronizer(store).sync(DAY, ['000001'], bulk_only=True)
    single.assert_not_called()
    live.assert_not_called()


@pytest.mark.parametrize('status', ['budget_exhausted', 'cooldown', 'busy'])
def test_bao_budget_or_cooldown_stops_immediately(store, monkeypatch, status):
    out = pd.DataFrame()
    out.attrs['status'] = status
    single = Mock(return_value=out)
    monkeypatch.setattr('data.valuation_sync.baostock.valuation_day', single)
    ValuationSynchronizer(store).sync(DAY, ['000001', '000002'])
    assert single.call_count == 1


def test_source_rejects_quality_and_wrong_date(store, monkeypatch):
    def fetch(provider, day):
        out = frame(pe_ttm=10, pb=2)
        if provider == 'zzshare':
            out.attrs['provenance'] = {'quality_status': 'unknown_effective_date'}
        else:
            out['trade_date'] = '2026-09-02'
        return out
    monkeypatch.setattr('data.valuation_sync.sources.historical', fetch)
    result = ValuationSynchronizer(store).sync(DAY, ['000001'], bulk_only=True)
    assert result['valuation_rows'] == 0
    assert [r['status'] for r in result['valuation_attempts']] == ['rejected_quality', 'rejected_date']


def test_zero_budget_and_busy_writer_make_no_calls(store, monkeypatch):
    from data.valuation_sync import _writer_slot
    fetch = Mock()
    monkeypatch.setattr('data.valuation_sync.sources.historical', fetch)
    with _writer_slot() as acquired:
        assert acquired
        result = ValuationSynchronizer(store).sync(DAY, ['000001'])
    assert result['valuation_attempts'][0]['status'] == 'busy'
    monkeypatch.setenv('VALUATION_EVENING_BUDGET_SECONDS', '0')
    monkeypatch.setenv('VALUATION_PREMARKET_BUDGET_SECONDS', '0')
    ValuationSynchronizer(store).sync(DAY, ['000001'])
    fetch.assert_not_called()


def test_tushare_unit_and_date_contract(monkeypatch):
    from data.sources import tushare
    api = Mock()
    api.daily_basic.return_value = pd.DataFrame([{
        'ts_code': '000001.SZ', 'trade_date': '20260901', 'pe_ttm': 10, 'pb': 2,
        'pe': 12, 'total_mv': 1000000, 'circ_mv': 900000,
    }])
    monkeypatch.setattr(tushare, 'available', lambda: True)
    monkeypatch.setattr(tushare, '_pro', lambda: api)
    monkeypatch.setattr(valuation, 'source_call', lambda *_: nullcontext())
    result = valuation.historical('tushare', DAY).iloc[0]
    assert result.market_cap == 100 and result.circulating_market_cap == 90
    assert result.trade_date == DAY and result.pe_lyr == 12


def test_live_does_not_request_historical_day(monkeypatch):
    spy = Mock(side_effect=AssertionError('no network'))
    monkeypatch.setattr(tencent, 'quotes', spy)
    assert valuation.live('tencent', ['000001'], '2001-01-01').empty
    spy.assert_not_called()


def test_quote_timestamp_and_cap_position(monkeypatch):
    values = ['0'] * 54
    values[1], values[30], values[44], values[45] = 'Sample', '20260901150100', '90', '100'
    monkeypatch.setattr(tencent.C, 'throttle', lambda *_: None)
    monkeypatch.setattr(tencent.C, 'http_get_text', lambda *a, **k: 'v_sz000001="' + '~'.join(values) + '";')
    quote = tencent.quotes(['000001'])['000001']
    assert quote['mcap_yi'] == 100 and quote['float_mcap_yi'] == 90
    assert quote['quote_time'] == '20260901150100'
    monkeypatch.setattr(eastmoney.C, 'http_get_json', lambda *a, **k: {'data': {'diff': [{
        'f12': '000001', 'f20': 1e10, 'f21': 9e9,
        'f124': int(datetime(2026, 9, 1, 15, 1, tzinfo=TZ).timestamp()),
    }]}})
    quote = eastmoney.ulist_quote(['000001'])['000001']
    assert closing_timestamp(quote['quote_time'], DAY)


def test_bao_endpoint_uses_dated_valuation_fields_and_shared_guard(monkeypatch):
    from data.sources import _baostock_deadline
    sdk = Mock()
    sdk.query_history_k_data_plus.return_value.error_code = '0'
    sdk.query_history_k_data_plus.return_value.next.side_effect = [True, False]
    sdk.query_history_k_data_plus.return_value.get_row_data.return_value = [DAY, 'sz.000001', '10', '2', '3', '4']
    guard = object()
    reserve = Mock()
    monkeypatch.setattr(baostock, '_provider_slot', lambda: nullcontext(guard))
    monkeypatch.setattr(baostock, '_ensure', lambda g: sdk if g is guard else None)
    monkeypatch.setattr(baostock, '_reserve_request', reserve)
    monkeypatch.setattr(baostock, 'source_call', lambda *_: nullcontext())
    monkeypatch.setattr(baostock, '_in_cooldown', lambda *_: False)
    monkeypatch.setattr(_baostock_deadline, 'sdk_deadline', lambda *_: nullcontext())
    out = baostock.valuation_day('000001', DAY)
    reserve.assert_called_once_with(guard, 'daily')
    args, kwargs = sdk.query_history_k_data_plus.call_args
    assert 'peTTM' in args[1] and kwargs == dict(start_date=DAY, end_date=DAY, frequency='d', adjustflag='3')
    assert out.attrs['status'] == 'ok' and 'pcf' not in out.columns


def test_socket_deadline_and_eof():
    from data.sources._baostock_deadline import DeadlineSocket
    sock = Mock()
    with pytest.raises(TimeoutError):
        DeadlineSocket(sock, 0).recv(8192)
    sock.recv.assert_not_called()
    import time
    sock.recv.return_value = b''
    with pytest.raises(ConnectionError):
        DeadlineSocket(sock, time.monotonic() + 1).recv(8192)


class CloseNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 1, 16, tzinfo=TZ)


def test_all_fallbacks_get_opportunity_and_formal_fields_win(store, monkeypatch):
    monkeypatch.setattr('data.valuation_sync.datetime', CloseNow)
    calls = []
    def history(provider, day):
        calls.append(provider)
        return frame('000001' if provider == 'zzshare' else '000002', pe_ttm=10, pb=2, market_cap=100)
    def live(provider, codes, day):
        calls.append(provider)
        return frame(codes[0], pb=9, market_cap=100) if codes else pd.DataFrame()
    monkeypatch.setattr('data.valuation_sync.sources.historical', history)
    monkeypatch.setattr('data.valuation_sync.sources.live', live)
    monkeypatch.setattr('data.valuation_sync.baostock.valuation_day',
                        lambda code, day: frame(code, pe_ttm=11, pb=3))
    result = ValuationSynchronizer(store).sync(DAY, [f'{n:06}' for n in range(1, 7)])
    assert calls == ['zzshare', 'tushare', 'tencent', 'mairui', 'moma', 'eastmoney']
    assert result['valuation_rows'] == 6
    saved = store.load_valuations(DAY).set_index('symbol')
    assert saved.loc['000001', 'pb'] == 2
    assert saved.loc['000003', 'pb'] == 3  # Bao outranks earlier Tencent


@pytest.mark.parametrize('provider', ['tencent', 'eastmoney', 'mairui', 'moma'])
def test_live_adapters_never_promote_ambiguous_pe(monkeypatch, provider):
    from data.sources import mairui, moma
    monkeypatch.setattr(valuation, 'datetime', CloseNow)
    monkeypatch.setattr(valuation, 'source_call', lambda *_: nullcontext())
    if provider in ('tencent', 'eastmoney'):
        quotes = {'000001': {'quote_time': '20260901150100', 'pb': 2,
                            'pe_ttm': 10, 'pe_static': 12, 'mcap_yi': 100, 'float_mcap_yi': 90}}
        monkeypatch.setattr(tencent, 'quotes', lambda *_: quotes)
        monkeypatch.setattr(eastmoney, 'ulist_quote', lambda *_: quotes)
    else:
        module = mairui if provider == 'mairui' else moma
        monkeypatch.setattr(module, 'available', lambda: True)
        monkeypatch.setattr(module._source.api, 'get', lambda *a, **k: [
            {'dm': '000001', 't': '2026-09-01 15:01:00', 'pb_ratio': 2, 'pe': 10, 'pv': 999999},
            {'dm': '000002', 't': '2026-08-31 15:01:00', 'pb_ratio': 2, 'pe': 10},
        ])
    result = valuation.live(provider, ['000001', '000002'], DAY)
    assert len(result) == 1 and result.iloc[0].pb == 2
    assert 'pe_ttm' not in result and 'pe_lyr' not in result
    if provider in ('mairui', 'moma'):
        assert 'market_cap' not in result


def test_empty_retry_keeps_previously_published_rows(store, monkeypatch):
    first = frame(pe_ratio=10, pb_ratio=2, market_cap=100)
    first.attrs['provenance'] = {'provider': 'zzshare', 'quality_status': 'ok'}
    store.upsert_valuations(first, as_of=DAY)
    old_id = store.load_valuations(DAY).iloc[0].dataset_id
    result = ValuationSynchronizer(store).sync(DAY, ['000001'])
    assert result['valuation_rows'] == 1
    assert store.load_valuations(DAY).iloc[0].dataset_id == old_id


def test_daily_sync_99_percent_with_multi_source_valuation_is_success(monkeypatch):
    from data.research_sync import ResearchSynchronizer
    store = Mock()
    store.daily_bar_symbol_count.return_value = 99
    store.load_latest_universe.return_value = pd.DataFrame({'symbol': [f'{n:06}' for n in range(100)]})
    daily = frame(close=10, volume=100)
    daily.attrs['provenance'] = {'quality_status': 'ok'}
    monkeypatch.setattr('data.research_sync.zzshare.get_market_daily', lambda *_: daily)
    monkeypatch.setattr('data.research_sync._normalize_market_bars', lambda f, _: f)
    summary = {'valuation_rows': 99, 'valuation_quality_status': 'ok',
               'valuation_field_coverage': {'pe_ttm': .99, 'pb': .99},
               'valuation_attempts': [{'provider': 'baostock', 'received': 99}]}
    spy = Mock(return_value=summary)
    monkeypatch.setattr(ValuationSynchronizer, 'sync', spy)
    result = ResearchSynchronizer(store).sync_day(DAY, fundamentals=False, fallback=False,
                                                 refresh_calendar=False, optional_fund_flow=False)
    assert result['quality_status'] == 'ok' and result['valuation_coverage'] == .99
    assert store.finish_sync.call_args.kwargs['status'] == 'success'
    assert store.finish_sync.call_args.kwargs['detail']['valuation_attempts'] == summary['valuation_attempts']


def test_current_snapshot_at_selection_threshold_is_not_labelled_lagged(monkeypatch):
    from data.research_sync import ResearchSynchronizer
    store = Mock()
    store.completed_sync.return_value = False
    store.load_universe.return_value = pd.DataFrame({'symbol': ['000001']})
    syncer = ResearchSynchronizer(store)
    monkeypatch.setattr(syncer, 'sync_day', lambda *a, **k: {
        'quality_status': 'incomplete', 'market_quality_status': 'ok', 'coverage': .99,
        'valuation_rows': 80, 'valuation_quality_status': 'ok', 'valuation_coverage': .8,
    })
    monkeypatch.setattr('data.research_sync.resolve_valuation',
                        lambda *a, **k: (pd.DataFrame(), {'ready': True, 'status': 'current'}))
    result = syncer.repair_daily_market_if_missing(DAY)
    assert result['selection_ready'] and not result['data_degraded']
    assert result['repair_mode'] == 'bulk_market_with_partial_valuation'
