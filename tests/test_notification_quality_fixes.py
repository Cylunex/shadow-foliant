"""Rendered payloads cross the actual router/archive boundary, with no transport."""
import ast
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
import pytest

from application.message_archive import MessageArchiveService
from application.portfolio_daily import build_daily, DailyExitService
from data.research_store import ResearchStore
from notify import notification_router as router
from notify.plain_language import compact_notification
from application.scheduled_report_contract import REPORT_MAX_BYTES

NOW = datetime(2026, 11, 9, 10, 5, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def archive(tmp_path, monkeypatch):
    store = ResearchStore(str(tmp_path / 'archive.db'), connect_fn=sqlite3.connect)
    service = MessageArchiveService(store=store, key=Fernet.generate_key())
    def action(name, body):
        if name == 'prepare':
            return service.prepare(**body)
        if name == 'start':
            return {'started': service.start(body['delivery_id'])}
        return {'recorded': service.finish(**body)}
    monkeypatch.setattr(router, 'archive_action', action)
    return service


@pytest.mark.parametrize('channel', ['qq', 'dingtalk'])
def test_digest_keeps_every_section_complete_original_and_one_delivery(tmp_path, monkeypatch, archive, channel):
    from core import postmarket_digest as digest
    monkeypatch.setattr(digest, '_DIR', str(tmp_path / 'digest'))
    monkeypatch.setenv('SHADOW_OIDC_REDIRECT_URI', 'https://example.com/stock/auth/callback')
    sources = {
        'pnl': ('已更新部分盈亏', '已更新部分盈亏 +100元\n基金 8/53只净值已更新\n累计按已更新数据统计'),
        'mx_close': ('妙想复盘', '未经核验的市净率76.37%【1】' * 600 + '原稿末尾完整保留'),
        'sector_rotation': ('题材轮动', '🔴 看多: 电力\n🟢 看空: 钢铁'),
        'eod_info': ('公告与研报', '利好2 600001 回购\n回购进展待核'),
        'strategy_scan': ('策略扫描', '股票池: 110只\n1. 600001 示例 — 命中2策略'),
        'strategy_evolution': ('策略评估', '✅ 反弹 胜率100% 均收益12% (触发17/51只)\n新变体' * 100),
    }
    for key, (title, body) in sources.items():
        digest.add_section(key, title, body, day='2026-11-09', max_chars=100)
    original = digest.format_digest('2026-11-09')
    body = digest.format_notification_digest('2026-11-09')
    assert '原稿末尾完整保留' in original
    assert all(title in body for title, _ in sources.values())
    assert '来源和指标口径待核验' in body and '市净率76.37%' not in body
    assert '非全市场实盘收益' in body and '51只' in body
    assert '8/53只净值已更新' in body
    assert 'https://example.com/stock/notifications' in body
    assert compact_notification('report', body) == body
    assert len(body.encode()) <= REPORT_MAX_BYTES
    sent = []
    monkeypatch.setitem(router.CHANNELS, channel, lambda title, content: (sent.append(content) or (True, 'HTTP 200')))
    kwargs = dict(source='jobs.jobs_hub', only_channels=[channel], original_body=original,
                  idempotency_key='postmarket:2026-11-09')
    first = router.send('report', '盘后汇总', body, **kwargs)
    router.send('report', '盘后汇总', body, **kwargs)
    assert sent == [body]
    detail = archive.detail(first.message_id)
    assert detail['original_body'] == original
    assert detail['deliveries'][0]['final_body'] == router._submitted_body(channel, '盘后汇总', body)


def test_weekly_qq_preserves_return_loss_pending_and_email_original(monkeypatch, archive):
    body = ('📊 AI 推荐评估周报 — 2026-11-08\n样本: 406 综合得分: 49.6 等级: D\n'
            '口径：未了结为浮动估值、已了结为实现收益；盈利占比不是已成交胜率。\n'
            '[综合选股] 样本=284 期间=30天\n综合得分:51\n'
            '样本盈利占比(含未了结浮动):55.6% 平均回报:-0.84% 最大亏损:-28.3%\n'
            '盈亏比:0.74 已了结:0 持有中:284\n' + '完整研究明细\n' * 100)
    sent = {}
    for ch in ('qq', 'email'):
        monkeypatch.setitem(router.CHANNELS, ch, lambda title, value, ch=ch: (sent.setdefault(ch, value) is not None, 'HTTP 200'))
    router.send('archive', 'AI 推荐周报', body, source='jobs.jobs_hub', only_channels=['qq', 'email'])
    assert all(value in sent['qq'] for value in ('-0.84%', '-28.3%', '持有中:284', '综合得分: 49.6'))
    assert '加密消息存档' not in sent['qq'] and '通知记录' in sent['qq']
    assert sent['email'] == body.strip()


def test_candidate_in_zone_explains_actual_blocker_without_loosening_gate():
    from jobs.intraday_decision_monitor import _candidate_decision
    plan = {'available': True, 'candidate_action': 'watch', 'entry_low': 13.36,
            'entry_high': 13.40, 'blockers': ['周线仍为空头环境', '预期盈亏比低于2.0门槛']}
    quote = {'price': 13.37, 'price_actionable': True}
    result = _candidate_decision(quote, plan, False)
    assert result['action'] == 'hold' and '周线仍为空头环境' in result['reason']
    assert _candidate_decision(quote, {**plan, 'candidate_action': 'buy'}, False)['action'] == 'add'
    assert _candidate_decision(quote, {**plan, 'candidate_action': 'buy'}, True)['action'] == 'data_insufficient'


def test_collection_start_does_not_discard_later_provider_quote_and_weekend_baseline(tmp_path):
    store = ResearchStore(str(tmp_path / 'daily.db'), connect_fn=sqlite3.connect)
    service = DailyExitService(store)
    holdings = [{'code': f'600{i:03}', 'quantity': 100} for i in range(16)]
    metadata = {'stock_symbols': {h['code'] for h in holdings}}
    friday = NOW - timedelta(days=3)
    row = {'symbol': '600000', 'action': 'hold', 'price_usable': True,
           'quote_time_source': 'provider', 'quote_as_of': friday.isoformat()}
    prior = build_daily(holdings, metadata, [row], now=friday, snapshot_at=friday - timedelta(seconds=30))
    service.deliver(prior, lambda *a, **kw: {'qq': (True, 'HTTP 200')})
    assert service.previous_accepted_report(NOW)['judgments']['600000']['action'] == 'hold'
    current = {**row, 'action': 'sell', 'reason': '跌破原计划止损', 'quote_as_of': NOW.isoformat()}
    report = build_daily(holdings, metadata, [current], now=NOW,
                         snapshot_at=NOW - timedelta(seconds=30), prior=service.previous_accepted_report(NOW))
    assert '上次11-06' in report['text'] and '今日10:05 退出复核' in report['text']
    assert '下次开市' not in report['text']
    stale = build_daily(holdings, metadata, [current], now=NOW, snapshot_at=friday)
    assert not stale['items'] and not stale['judgments']


@pytest.mark.parametrize('hour,minute,closed', [(9, 50, False), (14, 30, False), (10, 6, True)])
def test_daily_exit_does_not_run_outside_early_trading_window(monkeypatch, hour, minute, closed):
    from jobs import jobs_hub as hub
    import application.portfolio_daily as daily
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(hour=hour, minute=minute)
    monkeypatch.setattr(hub, 'datetime', Clock)
    monkeypatch.setattr(hub, '_skip_if_not_trading', lambda _: closed)
    monkeypatch.setattr(daily, 'run_daily_exit', lambda **kw: pytest.fail('must not send'))
    hub.task_daily_equity_exit()


def test_morning_exit_and_normal_report_share_one_message(monkeypatch):
    from jobs import jobs_hub as hub
    import application.portfolio_daily as daily
    from notify import portfolio_report
    calls = []
    import automation_config
    monkeypatch.setattr(automation_config, 'is_enabled', lambda _: True)
    monkeypatch.setattr(hub, '_push_daily', lambda *a: calls.append(a))
    monkeypatch.setattr(portfolio_report, 'render', lambda *a, **kw: '普通早盘')
    monkeypatch.setattr(daily, 'run_daily_exit', lambda **kw: {'status': 'accepted'})
    hub._publish_morning_intraday({'holdings': []})
    assert calls == []
    monkeypatch.setattr(daily, 'run_daily_exit', lambda **kw: {'status': 'not_required'})
    hub._publish_morning_intraday({'holdings': []})
    assert calls == [('A股早盘参考', '普通早盘')]
    monkeypatch.setattr(automation_config, 'is_enabled', lambda _: False)
    monkeypatch.setattr(daily, 'run_daily_exit', lambda **kw: pytest.fail('disabled review'))
    hub._publish_morning_intraday({'holdings': []})
    assert len(calls) == 2
    tree = ast.parse((Path(__file__).parents[1] / 'jobs/jobs_hub.py').read_text())
    schedules = {node.args[0].value: node.args[1].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == 'register' and len(node.args) > 1
                 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[1], ast.Constant)}
    assert schedules['daily_equity_exit'] == '10:06'


def test_merged_early_exit_keeps_etf_risk_and_shared_order_inside_budget():
    from notify.portfolio_report import render
    from application.holding_review import ordered_holdings
    stocks = [{'symbol': f'60000{i}', 'name': '很长的示例证券名称' * 10,
               'action': 'hold', 'requested_action': 'sell', 'requested_reason': '破位风险' * 100,
               'reason': '组合保护' * 100, 'cost_price': 12, 'price': 9,
               'holding_pnl_pct': -25, 'price_actionable': True} for i in range(5)]
    fund = {**stocks[0], 'symbol': '510001', 'name': '示例ETF', 'action': 'sell',
            'reason': '已触及原计划止损'}
    counts = {'equity_count': 39, 'etf_count': 11, 'other_fund_count': 1,
              'excess': 24, 'equity_symbols': [row['symbol'] for row in stocks]}
    snapshot = {'holdings': list(reversed(stocks)) + [fund], 'position_counts': counts,
                'generated_at': NOW.isoformat(), 'data_quality': {'status': 'success'}}
    body = render(snapshot, '10:05', exit_review={'counts': counts, 'text': '判断变化｜有效可比记录已保留'})
    assert '退出风险待处理' in body and '风险未解除' in body
    assert '其他持仓风险' in body and '示例ETF' in body and '已触及原计划止损' in body
    positions = [body.index(row['symbol']) for row in ordered_holdings(stocks)[:3]]
    assert positions == sorted(positions)
    assert len(body.encode()) <= REPORT_MAX_BYTES


def test_partial_nav_changes_both_pnl_title_and_rendered_body(tmp_path, monkeypatch):
    import sys
    from jobs import jobs_hub as hub
    from portfolio import daily_pnl
    import postmarket_digest as digest
    monkeypatch.setattr(digest, '_DIR', str(tmp_path / 'pnl'))
    monkeypatch.setattr(hub, '_skip_if_not_trading', lambda _: False)
    monkeypatch.setattr(hub, '_log_run', lambda *a, **kw: None)
    monkeypatch.setattr(daily_pnl, 'merge_save', lambda _: {
        'stock_count': 51, 'stock_daily_pnl': 565, 'fund_daily_pnl': 304,
        'fund_count': 53, 'fund_total_count': 53, 'fund_fresh_count': 8,
        'total_daily_pnl': 869, 'total_daily_pct': .18})
    monkeypatch.setattr(daily_pnl, 'get_summary', lambda _: {})
    sent = []
    monkeypatch.setitem(sys.modules, 'notification_router', SimpleNamespace(send=lambda *a, **kw: sent.append((a, kw))))
    hub.task_daily_pnl_snapshot()
    assert len(sent) == 1
    args, kwargs = sent[0]
    assert '已更新部分盈亏' in args[1] and '已更新部分盈亏' in args[2]
    assert '8/53' in args[2] and kwargs['original_body']
