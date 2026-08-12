import unittest

from analysis.market_structure import build_market_structure


class MarketStructureTests(unittest.TestCase):
    def test_hot_stock_and_board_match_produce_leader_role(self):
        context = {
            'base': {'info': {'industry': '通信设备'}},
            'sentiment': {
                'concept_blocks': {
                    'industry': ['通信设备'],
                    'concept': ['AI算力', '光模块'],
                },
                'hot_themes': [
                    {'theme': 'AI算力', 'count': 18},
                    {'theme': '机器人', 'count': 12},
                ],
                'hot_stocks': [
                    {'股票代码': '000001', '题材归因': 'AI算力+光模块'},
                ],
            },
        }
        result = build_market_structure(context, '000001')
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['primary_theme'], 'AI算力')
        self.assertEqual(result['stock_role'], 'leader')
        self.assertEqual(result['theme_phase'], 'unknown')

    def test_no_history_never_fabricates_theme_phase(self):
        result = build_market_structure({
            'base': {'info': {'industry': '银行'}},
        }, '000001')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['theme_phase'], 'unknown')
        self.assertTrue(any('历史题材' in item for item in result['limitations']))


if __name__ == '__main__':
    unittest.main()
