import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from analysis.selection_debate import _parse_verdict
from selection import strategy_cache


class DebateQualityTests(unittest.TestCase):
    def test_conflicting_buy_verdict_is_downgraded(self):
        answer = '结论:买入 | 置信:高 | 主因:盈利亏损且缺乏新增催化，风险较高'
        parsed = _parse_verdict(answer)
        self.assertEqual(parsed['verdict'], '谨慎')
        self.assertEqual(parsed['confidence'], '低')

    def test_missing_reason_is_rejected(self):
        self.assertIsNone(_parse_verdict('结论:买入 | 置信:高 | 主因:无'))


class StrategyCacheQualityTests(unittest.TestCase):
    def test_cache_only_does_not_call_external_fetch(self):
        calls = []

        def fetch():
            calls.append(1)
            return True, pd.DataFrame([{'code': '600519'}]), 'ok'

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(strategy_cache, '_cache_dir', return_value=tmp):
            ok, df, msg = strategy_cache.cached('缺失策略', fetch, cache_only=True)
        self.assertFalse(ok)
        self.assertIsNone(df)
        self.assertFalse(calls)
        self.assertIn('不重复请求', msg)


if __name__ == '__main__':
    unittest.main()
