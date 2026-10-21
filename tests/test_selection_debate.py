from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from analysis import selection_debate as debate


def test_verdict_parser_accepts_markdown_judge_but_keeps_risk_conflict_guard():
    text = '多头结论:买入\n**结论:**谨慎 | **置信:**中 | **主因:**必要事实仍需核实'
    assert debate._parse_verdict(text)['verdict'] == '谨慎'
    assert debate._parse_verdict('结论:买入 | 置信:高 | 主因:存在重大财务风险')['verdict'] == '谨慎'
    assert debate._parse_verdict('API返回空响应') is None


def test_short_debate_preserves_missing_price_and_change_as_unknown(monkeypatch):
    call = Mock(return_value='结论:谨慎 | 置信:低 | 主因:必要事实仍需核实')
    monkeypatch.setitem(sys.modules, 'deepseek_client', SimpleNamespace(
        DeepSeekClient=lambda: SimpleNamespace(call_api=call)))
    result = debate._debate_one('000001', {'name': '样例', 'price': None, 'change': None, 'pe': 0})
    assert result['verdict'] == '谨慎'
    prompt = call.call_args.args[0][1]['content']
    assert '现价未知 今日未知 PE未知' in prompt
    assert call.call_args.kwargs['timeout'] == 30
    assert call.call_args.kwargs['max_tokens'] == 1200


@pytest.mark.parametrize('completed,status', [(0, 'unavailable'), (1, 'partial'), (2, 'complete')])
def test_debate_records_partial_and_missing_results_without_false_success(monkeypatch, completed, status):
    monkeypatch.setitem(sys.modules, 'datahub', SimpleNamespace(
        quotes=lambda codes: {code: {'name': '样例'} for code in codes}, stock_names=lambda codes: {}))
    monkeypatch.setattr(debate, '_debate_one', lambda code, ctx: (
        {'verdict': '谨慎', 'confidence': '低', 'reason': '必要事实仍需核实'}
        if int(code[-1]) <= completed else None))
    result = debate.run_selection_debate(['000001', '000002'], record_signals=False)
    assert result['status'] == status and result['ok'] is (completed == 2)
    assert result['completed_count'] == completed and result['expected_count'] == 2
    assert len(result['missing_codes']) == 2 - completed
