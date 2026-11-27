from datetime import date, datetime
from io import BytesIO
from unittest.mock import MagicMock, Mock
from zipfile import ZipFile

from data import fred_economic_data, research_store
from jobs import jobs_hub, mx_advisor


def test_mx_wrap_skips_previous_day_before_database_or_model(monkeypatch):
    monkeypatch.setattr(jobs_hub, 'get_indicator_snapshot', lambda *args: {
        'trade_date': '2026-11-26', 'holdings': [{'symbol': '600001'}],
    })
    connect = Mock(side_effect=AssertionError('unexpected DB connection'))
    monkeypatch.setattr(mx_advisor, '_pg_conn', connect)
    result = mx_advisor.run_daily_wrap(now=datetime(2026, 11, 27, 17))
    assert result == {'status': 'skipped', 'reason': 'same_day_close_review_missing', 'report': ''}
    connect.assert_not_called()


def test_mx_wrap_keeps_live_actions_and_labels_pnl_pending(monkeypatch):
    from analysis import miaoxiang

    monkeypatch.setattr(jobs_hub, 'get_indicator_snapshot', lambda *args: {
        'trade_date': '2026-11-27', 'holdings': [{'symbol': '600001', 'action': 'reduce'}],
        'status': 'success', 'data_quality': {'coverage': 1},
    })
    monkeypatch.setattr(research_store.ResearchStore, 'latest_formal_selection', lambda self: {
        'selection_date': '2026-11-27', 'artifacts': {'formal_top5': {'payload': [{'symbol': '600002'}]}},
    })
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    monkeypatch.setattr(mx_advisor, '_pg_conn', lambda: conn)
    query = Mock(return_value={'content': '复核完成。'})
    monkeypatch.setattr(miaoxiang, 'query', query)

    result = mx_advisor.run_daily_wrap(now=datetime(2026, 11, 27, 17))

    sql, params = cursor.execute.call_args.args
    assert 'total_daily_pnl' in sql and 'WHERE snap_date' in sql and params == ('2026-11-27',)
    assert '600001' in query.call_args.args[1] and '600002' in query.call_args.args[1]
    assert result['status'] == 'partial' and result['reason'] == 'same_day_pnl_pending'
    assert '今日收益快照待夜间汇总' in result['report']


def test_public_fred_accepts_split_daily_export(monkeypatch):
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('daily.csv', 'observation_date,DGS10\n2026-11-25,4.21\n2026-11-28,5\n')
        archive.writestr('daily,_close.csv', 'observation_date,VIXCLS\n2026-11-25,23.2\n')
    response = Mock(content=stream.getvalue())
    monkeypatch.setattr(fred_economic_data.requests, 'get', Mock(return_value=response))
    result = fred_economic_data._public_daily(('DGS10', 'VIXCLS'), date(2026, 11, 27))
    assert result == {'DGS10': [{'date': '2026-11-25', 'value': 4.21}],
                      'VIXCLS': [{'date': '2026-11-25', 'value': 23.2}]}


def test_sp500_fallback_preserves_completed_session_dates(monkeypatch):
    response = Mock()
    response.json.return_value = {'data': {'code': 'SPX', 'klines': [
        '2026-11-24,100,100', '2026-11-25,100,101', '2026-11-27,101,120',
    ]}}
    monkeypatch.setattr(fred_economic_data.requests, 'get', Mock(return_value=response))
    assert fred_economic_data.eastmoney_sp500_close(date(2026, 11, 26)) == '标普500 2026-11-25: 101.00 (+1.00%)'
