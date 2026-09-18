"""OpenAPI shadow contracts; no provider access or secret material in tests."""
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
import io
import json
from unittest.mock import patch

from analysis.miaoxiang import diagnosis_verdict
from data.sources import iwencai_openapi as source
from data.sources import pywencai as legacy_source
from selection.wencai_query_contract import ORDER, QUERIES


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, chunk_size=8192):
        while chunk := self.raw.read(chunk_size):
            yield chunk


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_exact_queries_and_fixed_order():
    assert ORDER == ('低价擒牛', '低估值', '主力资金', '小市值', '净利增长')
    assert QUERIES['主力资金'] == '主力资金净流入排名'
    assert QUERIES['低价擒牛'].endswith('成交额由小至大排名')


def test_missing_key_has_no_network_request(monkeypatch):
    monkeypatch.delenv('IWENCAI_API_KEY', raising=False)
    monkeypatch.delenv('IWENCAI_API_KEY_FILE', raising=False)
    session = Session([])
    result = source.run_group('低价擒牛', session=session)
    assert result['status'] == 'credential_missing'
    assert session.calls == []


def test_trial_switch_blocks_legacy_before_transport(monkeypatch):
    monkeypatch.setenv('WENCAI_REFERENCE_SOURCE', 'openapi_trial')
    assert legacy_source.breaker_open()
    with patch.object(legacy_source, 'pywencai', create=True) as transport:
        try:
            legacy_source.pywencai_get('低价擒牛', group='低价擒牛')
            assert False, 'legacy call must be rejected'
        except legacy_source.PyWencaiRequestRejected:
            pass
        transport.assert_not_called()


def test_restricted_env_file_extracts_only_iwencai_key(tmp_path, monkeypatch):
    path = tmp_path / 'keys.env'
    path.write_text('OTHER_KEY=unrelated\nIWENCAI_API_KEY="test-key"\n')
    path.chmod(0o600)
    monkeypatch.delenv('IWENCAI_API_KEY', raising=False)
    monkeypatch.setenv('IWENCAI_API_KEY_FILE', str(path))
    assert source.configured_key() == 'test-key'
    path.chmod(0o644)
    assert source.configured_key() == ''


def test_pagination_schema_provenance_and_bounded_output():
    first = [{'股票代码': f'{i:06d}.SZ', '股票简称': '测试',
              '最新价[20260918]': 10} for i in range(1, 21)]
    second = [{'股票代码': f'{i:06d}.SZ', '股票简称': '测试',
               '最新价[20260918]': 10} for i in range(21, 23)]
    session = Session([
        Response({'datas': first, 'code_count': 22, 'chunks_info': {}}),
        Response({'datas': second, 'code_count': 22, 'chunks_info': {}}),
    ])
    with patch('data.sources.iwencai_openapi.provider_slot', return_value=nullcontext()):
        result = source.run_group('低价擒牛', session=session, key='test-key')
    assert result['status'] == 'semantic_unverified'
    assert result['parsed_conditions_verified'] is False
    assert result['target_top_n'] == 5
    assert result['top_n_coverage'] is True
    assert result['pagination_complete'] is True
    assert 'query_equivalence_not_audited' in result['failure_reasons']
    assert result['reported_count'] == 22
    assert result['returned_count'] == 22
    assert result['pages_fetched'] == 2
    assert result['data_as_of'] == '20260918'
    assert result['picks'] == [f'{i:06d}' for i in range(1, 6)]
    assert [call[1]['json']['page'] for call in session.calls] == ['1', '2']
    assert all(call[0] == source.URL for call in session.calls)
    assert all(call[1]['headers']['X-Claw-Skill-Id'] == source.SKILL_ID
               for call in session.calls)
    assert session.trust_env is False
    assert all(call[1]['proxies'] == {'http': '', 'https': '', 'all': ''}
               for call in session.calls)


def test_error_body_is_not_treated_as_empty_result():
    session = Session([Response({'code': 401, 'message': 'unauthorized'})])
    with patch('data.sources.iwencai_openapi.provider_slot', return_value=nullcontext()):
        result = source.run_group('低估值', session=session, key='test-key')
    assert result['status'] == 'invalid_schema'
    assert result['schema_valid'] is False
    assert 'message' not in result


def test_http_429_sets_cooldown_without_retry():
    session = Session([Response({}, status=429, headers={'Retry-After': '240'})])
    with patch('data.sources.iwencai_openapi.provider_slot', return_value=nullcontext()), \
         patch('data.sources.iwencai_openapi.set_provider_cooldown') as cooldown:
        result = source.run_group('低估值', session=session, key='test-key')
    assert result['status'] == 'http_429'
    assert len(session.calls) == 1
    cooldown.assert_called_once_with('iwencai_openapi', 240)


def test_premarket_sampling_cache_is_reused_after_formal_selection(tmp_path, monkeypatch):
    from jobs import jobs_hub as hub
    from data import research_store

    path = tmp_path / 'premarket.json'
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    calls = []
    saved = []
    sample = source.run_shadow(group_runner=lambda name: {
        'name': name, 'status': 'semantic_unverified',
        'picks': ['000001'], 'schema_valid': True, 'returned_count': 1,
    })

    class Store:
        def __init__(self, **_kwargs):
            pass

        def latest_formal_selection(self):
            return {
                'run_id': 'formal-1', 'selection_date': today,
                'artifacts': {'wencai_strategy_runs': {'payload': {
                    'strategies': {name: {
                        'status': 'ready', 'picks': [{'symbol': '000001'}],
                    } for name in ORDER},
                }}},
            }

        def save_selection_artifact(self, run_id, artifact_type, payload):
            saved.append((run_id, artifact_type, payload))

    monkeypatch.setattr(hub, '_skip_if_not_trading', lambda _job: False)
    monkeypatch.setattr(hub, '_wait_task_dependency', lambda *_args: True)
    monkeypatch.setattr(hub, '_log_run', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hub, '_iwencai_shadow_premarket_path', lambda: path)
    monkeypatch.setattr(hub, '_iwencai_shadow_isolated', lambda _old: (calls.append(1), sample)[1])
    monkeypatch.setattr(source, 'configured_key', lambda: 'present')
    monkeypatch.setattr(research_store, 'ResearchStore', Store)

    hub.task_iwencai_openapi_shadow_premarket()
    hub.task_iwencai_openapi_shadow_premarket()
    hub.task_iwencai_openapi_shadow()

    assert calls == [1]
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(saved) == 1
    assert saved[0][1] == 'iwencai_openapi_shadow'
    assert saved[0][2]['reference_affects_membership'] is False
    assert saved[0][2]['groups'][0]['observed_overlap_top5_count'] == 1
    assert saved[0][2]['groups'][0]['comparison_available'] is False


def test_shadow_comparison_never_promotes_to_formal():
    old = {'strategies': {name: {'status': 'ready', 'picks': [{'symbol': '000001'}]}
                          for name in ORDER}}
    shadow = source.run_shadow(old, group_runner=lambda name: {
        'name': name, 'status': 'complete', 'picks': ['000001', '000002']})
    assert shadow['ready_groups'] == 5
    assert all(row['overlap_top5_count'] == 1 for row in shadow['groups'])
    assert shadow['reference_affects_membership'] is False
    assert shadow['replacement_ready'] is False
    assert shadow['replacement_status'] == 'entitlement_or_semantic_blocked'
    assert shadow['valid_semantic_sample_day'] is False


def test_rank_ordinal_cannot_substitute_for_raw_amount():
    rows = [{'股票代码': f'{i:06d}.SZ', '股票简称': '测试',
             '最新价[20260918]': 10, '归母净利润同比增长率[20260630]': 120,
             '成交额[20260918]': None, '成交额排名名次[20260918]': i}
            for i in range(1, 21)]
    session = Session([Response({'datas': rows, 'code_count': 321})])
    with patch.object(source, 'MAX_PAGES', 1), \
         patch('data.sources.iwencai_openapi.provider_slot', return_value=nullcontext()):
        result = source.run_group('低价擒牛', session=session, key='test-key')
    assert result['required_numeric_fields'] is False
    assert result['sort_verified'] is False
    amount = result['field_evidence'][2]
    assert [item['field'] for item in amount['candidates']] == ['成交额[20260918]']
    assert result['top_n_coverage'] is True
    assert result['pagination_complete'] is False
    assert result['status'] == 'semantic_unverified'


def test_main_force_top_five_excludes_kcb_without_claiming_net_inflow_equivalence():
    rows = [{'股票代码': ('688001.SH' if i == 0 else f'{i:06d}.SZ'),
             '股票简称': '测试', '主力资金流向': 100 - i}
            for i in range(20)]
    session = Session([Response({'datas': rows, 'code_count': 5575})])
    with patch.object(source, 'MAX_PAGES', 1), \
         patch('data.sources.iwencai_openapi.provider_slot', return_value=nullcontext()):
        result = source.run_group('主力资金', session=session, key='test-key')
    assert result['picks'] == [f'{i:06d}' for i in range(1, 6)]
    assert result['stock_scope_verified'] is True
    assert result['scope_rejected_sample_count'] == 1
    assert result['capital_flow_metric_verified'] is False
    assert result['sort_field_as_of'] is None
    assert 'capital_flow_net_inflow_unverified' in result['failure_reasons']
    assert result['status'] == 'semantic_unverified'


def test_miaoxiang_error_dict_is_not_neutral():
    assert diagnosis_verdict({'error': 'HTTP 403', 'skill': 'stock_diagnosis'}) == ('诊断失败', '')
    assert diagnosis_verdict({'content': '建议观望'})[0] == '⚠️ 观望'
    assert diagnosis_verdict({'content': '建议规避'})[0] == '❌ 规避'
    assert diagnosis_verdict({'content': '暂不建议买入'})[0] == '⚠️ 观望'
