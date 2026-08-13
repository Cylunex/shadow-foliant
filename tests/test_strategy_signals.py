import unittest
import sys
import types
from unittest.mock import patch

import numpy as np
import pandas as pd

from analysis.strategy_signals import (
    evaluate_rise_rollover,
    rise_rollover_setup,
    rise_rollover_warning,
)


def _frame(close, volume=None):
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume if volume is not None else np.full(len(close), 100.0), dtype=float)
    return pd.DataFrame({
        'Open': close - 0.05,
        'High': close + 0.20,
        'Low': close - 0.20,
        'Close': close,
        'Volume': volume,
    }, index=pd.bdate_range('2026-01-05', periods=len(close)))


def _qualified_run():
    prices = [10.0]
    # 恰好 7 涨 3 跌，复合累计涨幅约 16.5%。
    for ret in (.03, .025, .03, -.01, .025, .02, -.01, .025, .025, -.005):
        prices.append(prices[-1] * (1 + ret))
    return prices


class RiseRolloverTests(unittest.TestCase):
    def test_setup_uses_ten_days_seven_up_and_fifteen_percent(self):
        setup = rise_rollover_setup(_frame(_qualified_run()))
        self.assertTrue(setup['setup'])
        self.assertEqual(setup['up_days'], 7)
        self.assertGreaterEqual(setup['cumulative_pct'], 15)

    def test_small_pullback_is_not_reported_as_sell_risk(self):
        setup = rise_rollover_setup(_frame(_qualified_run()))
        price = setup['reference_close'] * 0.992
        result = evaluate_rise_rollover(setup, price, -0.8, 0.9)
        self.assertEqual(result['level'], 0)
        self.assertEqual(result['risk_score'], 0)
        self.assertFalse(result['warning'])

    def test_first_weakness_only_watches(self):
        setup = rise_rollover_setup(_frame(_qualified_run()))
        price = setup['reference_close'] * 0.98
        result = evaluate_rise_rollover(setup, price, -2.0, 0.9)
        self.assertEqual(result['level'], 1)
        self.assertEqual(result['action'], 'watch')
        self.assertEqual(result['risk_score'], 0)

    def test_volume_confirms_and_hard_drop_escalates(self):
        setup = rise_rollover_setup(_frame(_qualified_run()))
        level2 = evaluate_rise_rollover(setup, setup['reference_close'] * 0.98, -2.0, 1.5)
        level3 = evaluate_rise_rollover(setup, setup['reference_close'] * 0.968, -3.2, 1.5)
        self.assertEqual(level2['level'], 2)
        self.assertEqual(level2['risk_score'], 1)
        self.assertEqual(level3['level'], 3)
        self.assertEqual(level3['risk_score'], 2)

    def test_daily_bar_warning_excludes_trigger_day_from_runup(self):
        close = _qualified_run() + [_qualified_run()[-1] * 0.98]
        volume = np.r_[np.full(len(close) - 1, 100.0), 150.0]
        result = rise_rollover_warning(_frame(close, volume))
        self.assertEqual(result['level'], 2)
        self.assertTrue(result['signal'])

    def test_below_threshold_never_arms_rollover(self):
        close = np.linspace(10.0, 11.2, 11)
        setup = rise_rollover_setup(_frame(close))
        self.assertFalse(setup['setup'])
        result = evaluate_rise_rollover(setup, 10.8, -3.5, 2.0)
        self.assertEqual(result['level'], 0)

    def test_holding_scan_only_scores_confirmed_rollover(self):
        from jobs import jobs_hub

        setup = rise_rollover_setup(_frame(_qualified_run()))
        portfolio_module = types.ModuleType('portfolio_db')
        portfolio_module.portfolio_db = types.SimpleNamespace(get_all_stocks=lambda: [{
            'code': '600000', 'name': '测试股', 'cost_price': 10.0, 'quantity': 100,
        }])
        with patch.dict(sys.modules, {'portfolio_db': portfolio_module}), \
                patch.object(jobs_hub, 'get_indicator_snapshot', return_value={
                    'rise_rollover_setup': setup,
                }), patch.object(jobs_hub.datahub, 'quotes') as quotes:
            quotes.return_value = {'600000': {
                'name': '测试股', 'price': setup['reference_close'] * 0.98,
                'change_pct': -2.0, 'vol_ratio': 0.9,
            }}
            watch = jobs_hub._scan_holdings_with_snapshot()[0]
            self.assertEqual(watch['rollover_level'], 1)
            self.assertEqual(watch['sell_score'], 0)

            quotes.return_value['600000']['vol_ratio'] = 1.5
            confirmed = jobs_hub._scan_holdings_with_snapshot()[0]
            self.assertEqual(confirmed['rollover_level'], 2)
            self.assertEqual(confirmed['sell_score'], 1)
            self.assertIn('连涨后转弱确认', confirmed['sell_reasons'])


if __name__ == '__main__':
    unittest.main()
