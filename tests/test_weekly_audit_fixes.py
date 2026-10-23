"""Regressions reproduced during the weekly production audit; no network."""
import json
from unittest.mock import Mock

import pandas as pd
import pytest

from application.results import clean_json
from data.sources import baidu
from fund import fund_service as service


def test_missing_pandas_values_are_json_null_before_profile_comparison(monkeypatch):
    import fund_data
    existing = {'code': '000001', 'basic': {'基金经理': '旧经理', '成立日期': None}}
    monkeypatch.setattr(service.ledger, 'list_saved', lambda _: [existing])
    saved = Mock()
    monkeypatch.setattr(service.ledger, 'save', saved)
    monkeypatch.setattr(fund_data, 'fund_name', lambda _: '测试基金')
    monkeypatch.setattr(fund_data, 'fund_type', lambda _: '混合型')
    monkeypatch.setattr(fund_data, 'get_fund_basic', lambda _: {
        '基金经理': pd.NA, '基金经理姓名': '新经理', '成立日期': pd.NaT, '规模': float('nan')})
    monkeypatch.setattr(fund_data, 'top_holdings', lambda _: {'holdings': []})
    result = service.refresh_profile('000001')
    assert result['manager'] == '新经理'
    assert result['basic']['基金经理'] is None
    assert result['basic']['成立日期'] is None
    assert {r['field'] for r in result['changes']} == {'基金经理', '基金经理姓名'}
    assert result['fetch_status'] == 'ready'
    json.dumps(result, allow_nan=False)
    assert clean_json([pd.NA, pd.NaT, float('nan')]) == [None, None, None]
    assert saved.call_count == 2


def test_one_failed_fund_review_does_not_starve_later_holdings(monkeypatch):
    import fund_db
    monkeypatch.setattr(service.ledger, 'list_saved', lambda _: [])
    saved = Mock()
    monkeypatch.setattr(service.ledger, 'save', saved)
    monkeypatch.setattr(fund_db, 'get_holdings', lambda: [
        {'code': '000001', 'shares': 1}, {'code': '000002', 'shares': 1}])
    monkeypatch.setattr(service, 'refresh_profile', Mock(side_effect=[
        TypeError('provider field'), {'fetch_status': 'ready'}]))
    results = service.review_week('2026-10-23')
    assert [r['status'] for r in results] == ['partial', 'ready']
    assert results[0]['error_type'] == 'TypeError'
    assert [c.args[0] for c in saved.call_args_list] == ['weekly_review_pending', 'weekly_review']


@pytest.mark.parametrize('payload', [None, [], 'error', {'ResultCode': 10003},
    {'ResultCode': 0, 'Result': 'error'}, {'ResultCode': 0, 'Result': {'message': 'error'}},
    {'ResultCode': 0, 'Result': [None, 'error', {'type': None, 'list': []}]}])
def test_baidu_malformed_response_has_empty_contract(monkeypatch, capsys, payload):
    session = Mock()
    session.get.return_value.json.return_value = payload
    monkeypatch.setattr(baidu.C, 'requests_session', lambda: session)
    assert baidu.concept_blocks('600001') == baidu._empty_blocks()
    assert 'object has no attribute' not in capsys.readouterr().out


def test_baidu_keeps_valid_siblings_and_does_not_classify_unknown_groups(monkeypatch):
    session = Mock()
    session.get.return_value.json.return_value = {'ResultCode': 0, 'Result': [
        'invalid', {'type': '概念', 'list': ['invalid', {'name': '测试概念'}, {'name': None}]},
        {'type': '行业', 'list': [{'name': '测试行业'}]},
        {'type': 'unknown', 'list': [{'name': '未知'}]}]}
    monkeypatch.setattr(baidu.C, 'requests_session', lambda: session)
    result = baidu.concept_blocks('600001')
    assert result['concept_tags'] == ['测试概念']
    assert [r['name'] for r in result['industry']] == ['测试行业']
