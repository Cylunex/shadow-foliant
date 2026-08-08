import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import datahub
import portfolio_policy
import postmarket_digest


class PortfolioPolicyTest(unittest.TestCase):
    @patch.dict(os.environ, {'PORTFOLIO_POSITION_MODE': 'high'})
    @patch('portfolio_policy.latest_market_add_signal', return_value=None)
    def test_high_position_blocks_automatic_buy_when_signal_missing(self, _signal):
        guarded = portfolio_policy.guard('buy', source_type='selection', reason='模型看多')
        self.assertTrue(guarded['blocked'])
        self.assertEqual(guarded['action'], 'watch')
        self.assertIn('高仓位总闸', guarded['reason'])

    @patch.dict(os.environ, {'PORTFOLIO_POSITION_MODE': 'high'})
    @patch('portfolio_policy.latest_market_add_signal', return_value={'must_add': True})
    def test_high_position_allows_buy_only_in_must_add_window(self, _signal):
        guarded = portfolio_policy.guard('add', source_type='analysis')
        self.assertFalse(guarded['blocked'])
        self.assertEqual(guarded['action'], 'add')

    @patch.dict(os.environ, {'PORTFOLIO_POSITION_MODE': 'high'})
    def test_manual_action_is_not_rewritten(self):
        guarded = portfolio_policy.guard('buy', source_type='manual')
        self.assertEqual(guarded['action'], 'buy')


class DataFreshnessTest(unittest.TestCase):
    def test_old_stale_kline_is_not_actionable(self):
        df = pd.DataFrame({'Close': [1.0, 2.0]})
        df.attrs.update(datahub_stale=True, datahub_cache_age_days=8,
                        datahub_source='stale_cache')
        quality = datahub.kline_quality(df, max_stale_days=3)
        self.assertTrue(quality['usable'])
        self.assertFalse(quality['actionable'])


class PostmarketDigestTest(unittest.TestCase):
    def test_sections_are_deduplicated_and_pnl_is_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(postmarket_digest, '_DIR', tmp):
                postmarket_digest.add_section('sector_rotation', '题材', '旧内容', day='2099-01-01')
                postmarket_digest.add_section('sector_rotation', '题材', '新内容', day='2099-01-01')
                postmarket_digest.add_section('pnl', '盈亏', '+100', day='2099-01-01')
                body = postmarket_digest.format_digest('2099-01-01')
        self.assertNotIn('旧内容', body)
        self.assertEqual(body.count('━━ 题材 ━━'), 1)
        self.assertLess(body.index('盈亏'), body.index('题材'))


class AgentWeekendStatusTest(unittest.TestCase):
    def test_weekend_policy_has_no_false_warning(self):
        # 具体周末快照日期逻辑已有 task_control 专项测试；这里锁住高仓位告警只在工作日展示。
        import inspect
        from jobs import task_control
        source = inspect.getsource(task_control.agent_cockpit)
        self.assertIn("weekday() < 5", source)


if __name__ == '__main__':
    unittest.main()
