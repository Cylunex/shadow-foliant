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
