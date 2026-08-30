import json
import unittest

from analysis.morning_strategy_report import (
    build_diagnosis,
    format_plain_morning_notification,
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


if __name__ == '__main__':
    unittest.main()
