import inspect
import unittest

from jobs import ai_recommendation_monitor
from portfolio.eod_review import _format


class EodMessageQualityTests(unittest.TestCase):
    def test_message_is_plain_and_limited_to_five_actions(self):
        items = []
        for i in range(8):
            items.append({
                'code': f'{i:06d}', 'name': f'股票{i}',
                'action': 'sell' if i < 3 else 'reduce',
                'exit_score': 80 - i, 'pnl': -8.2,
                'category': '破位减仓',
                'reason': '风险信号:破MA60(12.01)/缠论三卖/VaR95 6.4%',
            })
        text = _format(items, 72, 20, True)
        self.assertIn('尾盘只看这几件事', text)
        self.assertIn('跌破60日均线', text)
        self.assertIn('另外 3 只暂不展开', text)
        self.assertNotIn('尾盘强势', text)
        self.assertEqual(sum(f'{i}.' in text for i in range(1, 9)), 5)

    def test_post_close_outcome_no_longer_pushes_holding_targets(self):
        source = inspect.getsource(ai_recommendation_monitor.check_all_active)
        self.assertNotIn('monitored_stocks', source)
        self.assertNotIn('_notify(', source)


if __name__ == '__main__':
    unittest.main()
