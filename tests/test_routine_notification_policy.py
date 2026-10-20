import sqlite3
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest

from application.message_archive import MessageArchiveService
from data.research_store import ResearchStore
from notify import notification_router as router
from notify.plain_language import compact_notification


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    store = ResearchStore(str(tmp_path / "archive.db"), connect_fn=sqlite3.connect,
                          is_postgres=False, ensure_schema=True)
    service = MessageArchiveService(store=store, key=Fernet.generate_key())
    def archive(action, body):
        return (service.prepare(**body) if action == "prepare" else
                {"started": service.start(body["delivery_id"])} if action == "start" else
                {"recorded": service.finish(**body)})
    monkeypatch.setattr(router, "archive_action", archive)
    sent = []
    monkeypatch.setitem(router.CHANNELS, "qq", lambda title, body: (
        sent.append((title, body)) or (True, "HTTP 200")))
    return service, sent


@pytest.mark.parametrize("kind", ["morning", "selection", "portfolio"])
def test_real_task_reports_deliver_and_archive_once_without_critical_event(delivery, kind):
    from analysis.morning_strategy_report import format_plain_morning_notification
    from jobs.jobs_hub import _format_selection_reference_summary
    from notify.plain_language import build_portfolio_message

    if kind == "morning":
        title, body = format_plain_morning_notification(
            {}, premarket=True, as_of="2026-10-16 09:01")
    elif kind == "selection":
        rows = [{"code": f"600{i:03}", "name": f"候选{i}", "price": 10.0}
                for i in range(15)]
        title = "今日量化候选与数据状态"
        body = _format_selection_reference_summary(rows[:5], rows, {}, {})
    else:
        title, body = build_portfolio_message(
            label="早盘持仓", signal={"action": "hold"},
            sell_rows=[], buy_rows=[], market="等待核验", as_of="10:05")
    service, sent = delivery
    kwargs = {"source": "jobs.jobs_hub", "only_channels": ["qq"],
              "idempotency_key": f"task-report:{kind}:fixture"}
    first = router.send("report", title, body, **kwargs)
    second = router.send("report", title, body, **kwargs)
    assert first["qq"] == (True, "HTTP 200")
    assert first.archive_status == "recorded" and first.policy_status is None
    assert second["qq"] == (False, "archive_suppressed")
    assert sent == [(title, compact_notification("report", body))]
    detail = service.detail(first.message_id)
    assert detail["original_body"] == body
    assert detail["deliveries"][0]["status"] == "accepted"


@pytest.mark.parametrize('channel', ['qq', 'dingtalk'])
def test_maximum_premarket_facts_survive_real_rendering_archive_and_dedup(delivery, monkeypatch, channel):
    from analysis.morning_strategy_report import build_diagnosis, format_plain_morning_notification
    import notification_service
    sources = {'us_summary': '\n'.join(['道琼斯 2026-10-19: 45000.00 (+0.35%)'] * 20),
               'news_summary': '\n'.join(['[10-20 08:10] 政策新闻' + '完整事实' * 100] * 20)}
    diagnosis, meta = build_diagnosis('API调用失败: provider_unavailable', sources)
    title, body = format_plain_morning_notification(diagnosis, premarket=True,
        sources=sources, diagnosis_meta=meta, as_of='2026-10-20 09:00',
        holdings='💰 昨日收益(2026-10-19): 股票(56只) +590元(+0.29%) | '
                 '基金(10/53只净值已更新) +257元(+0.58%) | 合计 +847元(+0.17%)')
    service, sent = delivery
    requests = []
    monkeypatch.setattr(notification_service, 'NotificationService', lambda: SimpleNamespace(config={
        'webhook_enabled':True, 'webhook_type':'dingtalk', 'webhook_url':'https://example.com/webhook'}))
    def post(_url, **kwargs):
        requests.append(kwargs['json'])
        return SimpleNamespace(status_code=200, json=lambda: {'errcode':0})
    monkeypatch.setattr(router.requests, 'post', post)
    monkeypatch.setitem(router.CHANNELS, 'dingtalk', router._send_dingtalk_via_legacy)
    kwargs = {'source':'jobs.jobs_hub', 'only_channels':[channel],
              'idempotency_key':'morning-report:2026-10-20:fixture'}
    first = router.send('report', title, body, **kwargs)
    second = router.send('report', title, body, **kwargs)
    assert first[channel][0] is True
    assert second[channel] == (False, 'archive_suppressed')
    delivered = requests[0]['markdown']['text'] if channel == 'dingtalk' else sent[0][1]
    assert len(requests if channel == 'dingtalk' else sent) == 1
    detail = service.detail(first.message_id)
    assert detail['original_body'] == body
    assert detail['deliveries'][0]['final_body'] == delivered
    assert len(compact_notification('report', body).splitlines()) <= 8
    assert '道琼斯 2026-10-19' in delivered
    assert '政策新闻' in delivered and '模型暂不可用' in delivered
    assert '10/53只净值已更新' in delivered and '时间：2026-10-20 09:00' in delivered


@pytest.fixture
def wencai_sample(tmp_path):
    import json
    from selection.wencai_query_contract import ORDER

    payload = {
        'sampling_slot': 'premarket', 'executed_at': '2026-10-20T09:05:30+08:00',
        'expected_market_as_of': '2026-10-19',
        'groups': [{
            'name': name, 'status': 'complete', 'semantic_verified': True,
            'schema_valid': True, 'data_as_of': '20261019',
            'picks': [f'600{i * 10 + j:03}' for j in range(5)],
            'pick_details': [{'symbol': f'600{i * 10 + j:03}', 'name': '样例名称' * 10}
                             for j in range(5)],
        } for i, name in enumerate(ORDER)],
    }
    payload['groups'][3].update(status='semantic_unverified', semantic_verified=False)
    path = tmp_path / '2026-10-20-premarket.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path, payload


@pytest.mark.parametrize('channel', ['qq', 'dingtalk'])
def test_openapi_picks_survive_selection_delivery_archive_and_dedup(
        delivery, monkeypatch, channel, wencai_sample):
    from jobs.jobs_hub import _format_selection_reference_summary
    from selection.wencai_notification import load_premarket_reference
    from selection.wencai_query_contract import ORDER
    import notification_service

    path, _ = wencai_sample
    reference = load_premarket_reference(path, selection_date='2026-10-20',
                                        expected_market_as_of='2026-10-19')
    rows = [{'code': f'601{i:03}', 'name': f'正式候选{i}', 'price': 12.34}
            for i in range(15)]
    independent = {'status': 'ready', 'market_as_of': '2026-10-19',
                   'top5': rows[:5], 'top15': rows}
    body = _format_selection_reference_summary(
        rows[:5], rows, {}, {}, independent=independent,
        data_note='数据状态说明' * 40, wencai_reference=reference)
    original = _format_selection_reference_summary(
        rows[:5], rows, {}, {}, independent=independent,
        data_note='数据状态说明' * 40, wencai_reference=reference, compact_failures=False)
    service, sent = delivery
    requests = []
    monkeypatch.setattr(notification_service, 'NotificationService', lambda: SimpleNamespace(config={
        'webhook_enabled': True, 'webhook_type': 'dingtalk',
        'webhook_url': 'https://example.com/webhook'}))
    def post(_url, **kwargs):
        requests.append(kwargs['json'])
        return SimpleNamespace(status_code=200, json=lambda: {'errcode': 0})
    monkeypatch.setattr(router.requests, 'post', post)
    monkeypatch.setitem(router.CHANNELS, 'dingtalk', router._send_dingtalk_via_legacy)
    kwargs = {'source': 'jobs.jobs_hub', 'only_channels': [channel],
              'idempotency_key': 'selection-openapi:2026-10-20', 'original_body': original}
    first = router.send('report', '今日量化候选与数据状态', body, **kwargs)
    second = router.send('report', '今日量化候选与数据状态', body, **kwargs)
    assert first[channel][0] is True
    assert second[channel] == (False, 'archive_suppressed')
    delivered = requests[0]['markdown']['text'] if channel == 'dingtalk' else sent[0][1]
    assert len(requests if channel == 'dingtalk' else sent) == 1
    assert len(body) <= 900 and len(body.splitlines()) == 8
    assert '4/5组已核验' in delivered and '截至2026-10-19' in delivered
    for i, name in enumerate(ORDER):
        assert f'问财参考·{name}：' in delivered
        if i != 3:
            assert f'600{i * 10:03}' in delivered
    assert '小市值：⚠️条件或时点待核验' in delivered
    assert '600030' not in delivered
    assert '0/5可用' not in delivered
    detail = service.detail(first.message_id)
    assert detail['original_body'] == original
    assert detail['deliveries'][0]['final_body'] == delivered
    assert rows[0]['code'] == '601000' and reference['reference_affects_membership'] is False


@pytest.mark.parametrize('invalid', ['yesterday', 'market_date', 'duplicate_group', 'naive', 'broken'])
def test_selection_notification_rejects_invalid_premarket_sample(wencai_sample, invalid):
    import json
    from selection.wencai_notification import load_premarket_reference, format_reference_lines

    path, payload = wencai_sample
    if invalid == 'yesterday':
        payload['executed_at'] = '2026-10-19T09:05:30+08:00'
    elif invalid == 'market_date':
        payload['expected_market_as_of'] = '2026-10-16'
    elif invalid == 'duplicate_group':
        payload['groups'][4]['name'] = payload['groups'][0]['name']
    elif invalid == 'naive':
        payload['executed_at'] = '2026-10-20T09:05:30'
    path.write_text('broken' if invalid == 'broken' else json.dumps(payload), encoding='utf-8')
    reference = load_premarket_reference(path, selection_date='2026-10-20',
                                        expected_market_as_of='2026-10-19')
    assert reference['ready_groups'] == 0
    assert reference['availability_reason'] == 'premarket_cache_invalid'
    assert all('盘前缓存无效' in line for line in format_reference_lines(reference))


def test_selection_notification_missing_cache_never_calls_provider(tmp_path, monkeypatch):
    from selection.wencai_notification import load_premarket_reference, format_reference_lines
    def forbidden(*args, **kwargs):
        pytest.fail('selection notification must only read the premarket cache')
    monkeypatch.setattr('data.sources.iwencai_openapi.run_group', forbidden)
    reference = load_premarket_reference(tmp_path / 'missing.json', selection_date='2026-10-20',
                                        expected_market_as_of='2026-10-19')
    assert reference['ready_groups'] == 0
    assert all('盘前缓存缺失' in line for line in format_reference_lines(reference))


@pytest.mark.parametrize("source", [
    "monitor.monitor_service", "monitor_scheduler", "smart_monitor_deepseek",
    "jobs.ai_recommendation_monitor", "jobs.intraday_decision_monitor",
])
@pytest.mark.parametrize("category", ["report", "archive", "alert", "daily_summary"])
def test_monitors_cannot_relabel_frequent_messages_as_reports(delivery, source, category):
    _, sent = delivery
    result = router.send(category, "监控变化", "普通行情变化",
                         source=source, only_channels=["qq"])
    assert not result and result.policy_status == "silent_critical_only"
    assert sent == []


@pytest.mark.parametrize("source", ["jobs_hub", "core.briefing", "jobs.mx_advisor"])
def test_routine_report_import_aliases_do_not_require_monitor_certification(delivery, source):
    _, sent = delivery
    result = router.send("report", "正常分析", "报告正文",
                         source=source, only_channels=["qq"])
    assert result["qq"][0] and len(sent) == 1
