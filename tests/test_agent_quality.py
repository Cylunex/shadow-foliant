import unittest

from core.agent_contract import context_quality


class AgentContextQualityTests(unittest.TestCase):
    def test_quality_is_normalized_to_requested_groups(self):
        quality = context_quality({
            'base': {'info': {'name': '测试股'}, 'errors': [],
                     '_meta': {'duration_ms': 12}},
            'kline_technical': {
                'indicators': {'MA20': 10},
                'data_quality': {'usable': True, 'actionable': True, 'stale': False},
                'errors': [], '_meta': {'duration_ms': 30},
            },
        })
        self.assertEqual(quality['overall_score'], 100)
        self.assertFalse(quality['core_degraded'])
        self.assertTrue(quality['guardrails']['directional_action_allowed'])
        self.assertEqual(quality['stages'][1]['duration_ms'], 30)

    def test_stale_kline_caps_confidence_and_blocks_directional_action(self):
        quality = context_quality({
            'base': {'info': {'name': '测试股'}, 'errors': []},
            'kline_technical': {
                'indicators': {'MA20': 10},
                'data_quality': {'usable': True, 'actionable': False,
                                 'stale': True, 'reason': 'stale_cache_4.0d'},
                'errors': [],
            },
        })
        self.assertEqual(quality['blocks']['kline_technical']['status'], 'stale')
        self.assertTrue(quality['core_degraded'])
        self.assertEqual(quality['guardrails']['confidence_cap'], 'medium')
        self.assertFalse(quality['guardrails']['directional_action_allowed'])

    def test_optional_group_failure_is_partial_not_core_degradation(self):
        quality = context_quality({
            'base': {'info': {'name': '测试股'}, 'errors': []},
            'sentiment': {'news': [{'title': 'x'}], 'errors': ['margin: timeout']},
        })
        self.assertEqual(quality['blocks']['sentiment']['status'], 'partial')
        self.assertFalse(quality['core_degraded'])

    def test_partial_core_caps_confidence_without_blocking_usable_direction(self):
        quality = context_quality({
            'base': {'info': {'name': '测试股'}, 'errors': []},
            'kline_technical': {
                'indicators': {'MA20': 10},
                'data_quality': {'usable': True, 'actionable': True, 'stale': False},
                'errors': ['pattern_detect: optional failure'],
            },
        })
        self.assertTrue(quality['core_degraded'])
        self.assertFalse(quality['core_unusable'])
        self.assertEqual(quality['guardrails']['confidence_cap'], 'medium')
        self.assertTrue(quality['guardrails']['directional_action_allowed'])


if __name__ == '__main__':
    unittest.main()
