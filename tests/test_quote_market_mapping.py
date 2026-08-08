import unittest

from data.sources import _common as common
from portfolio.portfolio_snapshot import _resolve_price


class QuoteMarketMappingTest(unittest.TestCase):
    def test_exchange_mapping_covers_shanghai_etf_and_beijing_codes(self):
        self.assertEqual(common.a_prefix('563800'), 'sh')
        self.assertEqual(common.tencent_code('563800'), 'sh563800')
        self.assertEqual(common.sina_code('563800'), 'sh563800')
        self.assertEqual(common.em_secid('563800'), '1.563800')
        self.assertEqual(common.a_prefix('920819'), 'bj')
        self.assertEqual(common.tencent_code('920819'), 'bj920819')

    def test_snapshot_rejects_implausible_quote_jump(self):
        price, source, anomalous = _resolve_price(100, 1.2989, 1.2989)
        self.assertEqual(price, 1.2989)
        self.assertEqual(source, 'previous_anomaly')
        self.assertTrue(anomalous)

    def test_snapshot_uses_previous_price_when_quote_is_missing(self):
        price, source, anomalous = _resolve_price(None, 4.059, 3.8)
        self.assertEqual(price, 4.059)
        self.assertEqual(source, 'previous_missing')
        self.assertFalse(anomalous)


if __name__ == '__main__':
    unittest.main()
