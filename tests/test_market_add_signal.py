import unittest

from analysis.market_add_signal import evaluate


def _indices(*changes):
    names = ['上证指数', '深证成指', '创业板指', '科创50', '沪深300']
    return [{'name': name, 'change_pct': change} for name, change in zip(names, changes)]


class MarketAddSignalTests(unittest.TestCase):
    def test_small_decline_does_not_trigger_add(self):
        r = evaluate(_indices(-0.4, -0.8, -1.0, -0.6, -0.5), [-0.2, 0.3, -0.1], 1.01)
        self.assertEqual(r['action'], 'hold')
        self.assertFalse(r['must_add'])

    def test_isolated_broad_sharp_drop_triggers_must_add(self):
        r = evaluate(_indices(-1.9, -2.4, -2.8, -2.1, -1.8), [0.4, -0.2, 0.1], 0.99)
        self.assertEqual(r['action'], 'strong_buy')
        self.assertTrue(r['must_add'])

    def test_consecutive_sharp_drop_blocks_add(self):
        r = evaluate(_indices(-2.0, -2.5, -3.0, -2.2, -1.9), [0.2, -0.3, -1.8], 0.98)
        self.assertEqual(r['action'], 'reduce')
        self.assertFalse(r['must_add'])

    def test_broken_trend_blocks_add_even_when_today_is_sharp(self):
        r = evaluate(_indices(-2.0, -2.5, -3.0, -2.2, -1.9), [0.2, 0.1, -0.3], 0.92)
        self.assertEqual(r['action'], 'reduce')
        self.assertFalse(r['must_add'])

    def test_insufficient_market_data_is_conservative(self):
        r = evaluate(_indices(-2.0, -2.5), [0.1], 1.0)
        self.assertEqual(r['level'], 'unknown')
        self.assertFalse(r['must_add'])

    def test_moderate_broad_dip_is_buy(self):
        r = evaluate(_indices(-0.9, -1.1, -1.0, -0.7, -0.8), [0.2, -0.1, 0.1], 1.0)
        self.assertEqual(r['action'], 'buy')
        self.assertEqual(r['action_rank'], 1)

    def test_cascade_and_broken_trend_is_sell(self):
        r = evaluate(_indices(-2.2, -2.6, -3.0, -2.4, -2.0), [-1.2, -0.5, -1.8], 0.92)
        self.assertEqual(r['action'], 'sell')
        self.assertEqual(r['action_rank'], -2)

    def test_broad_rally_trims_high_position(self):
        r = evaluate(_indices(2.0, 2.2, 1.9, 2.5, 1.8), [0.1, -0.1, 0.2], 1.03)
        self.assertEqual(r['action'], 'reduce')

    def test_available_breadth_confirms_broad_dip(self):
        breadth = {'available': True, 'covered': 480, 'up_count': 100,
                   'up_ratio': 100 / 480, 'down_ratio': 380 / 480}
        r = evaluate(_indices(-0.9, -1.1, -1.0, -0.7, -0.8),
                     [0.2, -0.1, 0.1], 1.0, breadth=breadth)
        self.assertEqual(r['action'], 'buy')
        self.assertTrue(r['breadth']['available'])

    def test_breadth_disagreement_prevents_false_broad_dip(self):
        breadth = {'available': True, 'covered': 480, 'up_count': 380,
                   'up_ratio': 380 / 480, 'down_ratio': 100 / 480}
        r = evaluate(_indices(-0.9, -1.1, -1.0, -0.7, -0.8),
                     [0.2, -0.1, 0.1], 1.0, breadth=breadth)
        self.assertEqual(r['action'], 'hold')


if __name__ == '__main__':
    unittest.main()
