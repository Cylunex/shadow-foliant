import sqlite3

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
