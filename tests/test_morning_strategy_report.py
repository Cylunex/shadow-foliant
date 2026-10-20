import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from analysis.morning_strategy_report import (
    build_diagnosis,
    format_plain_morning_notification,
    format_us_daily_reference,
    format_previous_pnl,
    parse_diagnosis,
)


def _sources(**overrides):
    data = {
        'dragon_tiger_summary': '000001 示例 净1200万 涨1.2%',
        'us_summary': '道琼斯: 45000 (+0.35%)\n标普500: 6500 (-0.10%)',
        'news_summary': '[08:10] 重要政策新闻',
        'hot_summary': '000001 示例 +5.2% — 人工智能',
        'themes_summary': '人工智能 (5 只)\n机器人 (3 只)',
        'fred_summary': 'VIX恐慌指数: 16.2 (-1.20%)',
        'cn_index_summary': '上证指数-0.35%  深证成指-0.62%  创业板指-0.40%',
        'sector_summary': '强势: 通信设备1.2%、工业金属0.8%\n弱势: 医药-1.0%',
        'hold_summary': '扫描 8 只持仓(盘后快照),无显著买卖信号',
    }
    data.update(overrides)
    return data


class MorningStrategyReportTests(unittest.TestCase):
    def test_parses_fenced_json_with_leading_reasoning(self):
        payload = {
            'open_strategy': '先观察', 'external_impact': '外盘中性',
            'hot_sectors': ['通信'], 'risk_warning': '不要追高', 'confidence': '中',
        }
        raw = '分析完成。\n```json\n' + json.dumps(payload, ensure_ascii=False) + '\n```'
        self.assertEqual(parse_diagnosis(raw)['open_strategy'], '先观察')

    def test_empty_response_builds_complete_rule_report(self):
        diagnosis, meta = build_diagnosis('API返回空响应', _sources())
        self.assertEqual(meta['mode'], 'rules')
        self.assertEqual(meta['reason'], 'empty_response')
        self.assertEqual(meta['coverage']['available'], 9)
        for key in ('lazy_summary', 'open_strategy', 'external_impact',
                    'hot_sectors', 'risk_warning', 'confidence'):
            self.assertTrue(diagnosis[key])
        self.assertEqual(diagnosis['candidate_stocks'], [])
        self.assertNotIn('（无）', json.dumps(diagnosis, ensure_ascii=False))

    def test_timeout_is_observable_and_low_coverage_is_low_confidence(self):
        missing = {key: '（无数据）' for key in _sources()}
        diagnosis, meta = build_diagnosis('API调用失败: Request timed out.', missing)
        self.assertEqual(meta['reason'], 'timeout')
        self.assertEqual(meta['coverage']['available'], 0)
        self.assertEqual(diagnosis['confidence'], '低')
        self.assertEqual(diagnosis['hot_sectors'], ['暂无可靠板块信号，开盘后观察量价确认'])

    def test_partial_json_uses_hybrid_fallback_and_drops_candidates(self):
        raw = json.dumps({
            'open_strategy': 'AI 开盘建议',
            'candidate_stocks': [{'code': '000001', 'rating': 'buy'}],
            'confidence': '高',
        }, ensure_ascii=False)
        diagnosis, meta = build_diagnosis(raw, _sources())
        self.assertEqual(meta['mode'], 'hybrid')
        self.assertEqual(diagnosis['open_strategy'], 'AI 开盘建议')
        self.assertTrue(diagnosis['external_impact'])
        self.assertEqual(diagnosis['candidate_stocks'], [])
        self.assertEqual(diagnosis['confidence'], '中')

    def test_complete_json_keeps_ai_result_and_candidates(self):
        raw = json.dumps({
            'lazy_summary': '今天先稳住。',
            'open_strategy': '低开观察，高开不追。',
            'external_impact': '外盘中性。',
            'hot_sectors': ['通信（有催化）'],
            'risk_warning': '防止冲高回落。',
            'candidate_stocks': [{'code': '000001', 'rating': 'hold'}],
            'confidence': '高',
        }, ensure_ascii=False)
        diagnosis, meta = build_diagnosis(raw, _sources())
        self.assertEqual(meta['mode'], 'ai')
        self.assertEqual(meta['reason'], 'ok')
        self.assertEqual(diagnosis['confidence'], '高')
        self.assertEqual(diagnosis['candidate_stocks'][0]['code'], '000001')
        self.assertIn(diagnosis['market_direction'], ('看涨', '看跌', '震荡'))
        self.assertIn(diagnosis['position_action'], ('加仓', '减仓', '不动'))

    def test_rule_report_extracts_real_sector_leaders(self):
        diagnosis, _ = build_diagnosis('', _sources())
        self.assertEqual(diagnosis['hot_sectors'][:2], ['通信设备1.2%', '工业金属0.8%'])
        self.assertIn('隔夜美股', diagnosis['external_impact'])
        self.assertNotIn('北向', diagnosis['external_impact'])

    def test_plain_notification_only_leads_with_direction_and_action(self):
        diagnosis, _ = build_diagnosis('', _sources(
            cn_index_summary='上证指数-1.50% 深证成指-1.80% 创业板指-1.30%',
        ))
        title, body = format_plain_morning_notification(
            diagnosis, market='上证指数-1.50%', holdings='两只持仓走势转弱', as_of='09:00'
        )
        self.assertEqual(title, '🟢 早盘判断：看跌｜减仓')
        self.assertEqual(body.splitlines()[0], '操作：减仓')
        self.assertNotIn('北向', title + body)

    def test_premarket_notification_does_not_interpret_zero_quotes_or_truncate(self):
        title, body = format_plain_morning_notification(
            {'market_direction': '震荡', 'position_action': '不动'},
            market='深证成指0.00 (+0.00%) 创业板指0.00 (+0.00%)',
            holdings='💰 昨日收益(2026-09-23): 合计 -1,048元(-0.20%)\n建议卖出若干',
            as_of='2026-09-24 09:02', premarket=True,
        )
        self.assertEqual(title, '🌅 盘前简报')
        self.assertIn('尚未开盘', body)
        self.assertIn('昨日收益', body)
        self.assertNotIn('0.00', body)
        self.assertNotIn('建议卖出', body)
        self.assertNotIn('…', body)

    def test_premarket_includes_collected_facts_and_distinguishes_model_failure(self):
        sources = _sources(cn_index_summary='盘前数据未形成；开盘后确认',
                           sector_summary='盘前数据未形成；开盘后确认')
        diagnosis, meta = build_diagnosis('API调用失败: provider_unavailable', sources)
        self.assertEqual(meta['reason'], 'provider_unavailable')
        self.assertEqual(meta['coverage']['available'], 7)
        self.assertEqual(meta['coverage']['pending'], ['A股指数', '行业板块'])
        self.assertEqual(meta['coverage']['missing'], [])
        title, body = format_plain_morning_notification(
            diagnosis, premarket=True, sources=sources, diagnosis_meta=meta,
            holdings='💰 昨日收益(2026-10-19): 基金(10/53只净值已更新) +257元',
            as_of='2026-10-20 09:00')
        self.assertEqual(title, '🌅 盘前简报')
        self.assertIn('道琼斯', body)
        self.assertIn('重要政策新闻', body)
        self.assertIn('模型暂不可用', body)
        self.assertIn('10/53只净值已更新', body)
        self.assertNotIn('JSON', body)
        self.assertNotIn('操作：', body)
        self.assertLessEqual(len(body.splitlines()), 8)

    def test_us_daily_reference_keeps_date_aligned_after_missing_close(self):
        days = [datetime(2026, 10, day, 9, 30, tzinfo=ZoneInfo('America/New_York')).timestamp()
                for day in (15, 16, 19)]
        result = {'timestamp': days, 'indicators': {'quote': [{'close': [100, 102, None]}]}}
        self.assertEqual(format_us_daily_reference('示例指数', result),
                         '示例指数 2026-10-16: 102.00 (+2.00%)')
        result.pop('timestamp')
        self.assertIn('日期未核实', format_us_daily_reference('示例指数', result))

    def test_missing_premarket_sources_are_not_reported_as_live_zeroes(self):
        _, body = format_plain_morning_notification(
            {}, premarket=True, sources={'us_summary': '(拉取失败: timeout)',
                                         'news_summary': '（盘前事实流暂无可用事件）'})
        self.assertIn('本次未获取', body)
        self.assertIn('本次暂无可用要闻', body)
        self.assertNotIn('timeout', body)

    def test_partial_fund_navs_do_not_claim_complete_daily_profit(self):
        snapshot = {'snap_date':'2026-10-19','stock_count':56,'stock_daily_pnl':590,
                    'stock_daily_pct':0.29,'fund_count':53,'fund_total_count':53,
                    'fund_fresh_count':10,'fund_daily_pnl':257,'fund_daily_pct':0.58,
                    'total_daily_pnl':847,'total_daily_pct':0.17}
        text = format_previous_pnl(snapshot)
        self.assertIn('10/53只净值已更新', text)
        self.assertIn('已更新部分合计 +847元', text)
        snapshot['fund_fresh_count'] = 53
        self.assertNotIn('已更新部分合计', format_previous_pnl(snapshot))


if __name__ == '__main__':
    unittest.main()
