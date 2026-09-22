import inspect
import unittest
from datetime import date, timedelta

from jobs import ai_recommendation_monitor
from portfolio.eod_review import _apply_portfolio_action_guard, _format
from portfolio.exit_advisor import _exit_score, _holding_age_evidence


class EodMessageQualityTests(unittest.TestCase):
    def test_tracking_created_at_is_not_treated_as_holding_age(self):
        old = (date.today() - timedelta(days=120)).isoformat()
        age = _holding_age_evidence({"created_at": old})
        self.assertIsNone(age["holding_days"])
        self.assertEqual(age["tracking_days"], 120)
        self.assertEqual(age["basis"], "unconfirmed_tracking_date")

        explicit = _holding_age_evidence({"created_at": old, "holding_since": old})
        self.assertEqual(explicit["holding_days"], 120)
        self.assertEqual(explicit["basis"], "explicit_position_open_date")

    def test_long_flat_holding_is_observation_not_sell_trigger(self):
        _, category, action, _ = _exit_score(
            {"pnl": 2, "change": 0, "sell_score": 0}, 120,
        )
        self.assertEqual(category, "长期效率观察")
        self.assertEqual(action, "hold")

        _, category, action, _ = _exit_score(
            {"pnl": 2, "change": 0, "sell_score": 2}, 120,
        )
        self.assertEqual(category, "破位减仓")
        self.assertEqual(action, "reduce")

    def test_portfolio_guard_caps_sell_and_total_action_counts(self):
        items = [
            {"code": str(i), "action": "sell", "category": f"trigger-{i}",
             "position_value": 100, "reason": "risk", "decision_source": "formal_signal"}
            for i in range(5)
        ]
        guarded, meta = _apply_portfolio_action_guard(
            items, max_sells=2, max_actions=4, max_turnover_pct=1.0,
            max_same_trigger=10,
        )
        self.assertEqual(meta["sell_count"], 2)
        self.assertEqual(meta["action_count"], 4)
        self.assertEqual([row["action"] for row in guarded],
                         ["sell", "sell", "reduce", "reduce", "hold"])
        self.assertTrue(guarded[-1]["action_guard"]["changed"])
        self.assertEqual(guarded[-1]["original_action"], "sell")
        self.assertEqual(guarded[-1]["original_reason"], "risk")
        self.assertNotIn("position_value", guarded[0])

    def test_portfolio_guard_caps_trigger_concentration_and_turnover(self):
        items = [
            {"code": str(i), "action": "reduce", "category": "same-trigger",
             "position_value": 100, "reason": "risk"}
            for i in range(5)
        ]
        concentrated, meta = _apply_portfolio_action_guard(
            items, max_sells=10, max_actions=10, max_turnover_pct=1.0,
            max_same_trigger=2,
        )
        self.assertEqual(meta["action_count"], 2)
        self.assertEqual([row["action"] for row in concentrated],
                         ["reduce", "reduce", "hold", "hold", "hold"])

        sell_items = [
            {"code": str(i), "action": "sell", "category": f"trigger-{i}",
             "position_value": 100, "reason": "risk"}
            for i in range(5)
        ]
        turnover_guarded, turnover_meta = _apply_portfolio_action_guard(
            sell_items, max_sells=10, max_actions=10, max_turnover_pct=.10,
            max_same_trigger=10,
        )
        self.assertEqual(turnover_meta["estimated_turnover_pct"], 10.0)
        self.assertEqual([row["action"] for row in turnover_guarded],
                         ["reduce", "reduce", "hold", "hold", "hold"])

        unknown, _ = _apply_portfolio_action_guard(
            [{"code": "unknown", "action": "sell", "category": "risk",
              "position_value": None, "reason": "risk"},
             {"code": "known", "action": "hold", "category": "healthy",
              "position_value": 100, "reason": "ok"}],
            max_sells=10, max_actions=10, max_turnover_pct=1.0,
            max_same_trigger=10,
        )
        self.assertEqual(unknown[0]["action"], "hold")
        self.assertIn("turnover_denominator_unavailable",
                      unknown[0]["action_guard"]["reasons"])

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

    def test_guarded_hold_keeps_original_trigger_in_fallback_message(self):
        text = _format([{
            'code': '603271', 'name': '永杰新材', 'action': 'hold',
            'original_action': 'reduce', 'original_reason': '触及第一目标39.79',
            'action_guard': {'changed': True}, 'exit_score': 50,
        }], 1, 20, False)
        self.assertIn('原始触发减仓(触及第一目标39.79)→最终不动', text)
        self.assertIn('保护不代表风险解除', text)

    def test_post_close_outcome_no_longer_pushes_holding_targets(self):
        source = inspect.getsource(ai_recommendation_monitor.check_all_active)
        self.assertNotIn('monitored_stocks', source)
        self.assertNotIn('_notify(', source)


if __name__ == '__main__':
    unittest.main()
