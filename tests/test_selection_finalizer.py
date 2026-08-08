import unittest

from analysis.selection_finalizer import finalize_selection, format_final_selection


class SelectionFinalizerTests(unittest.TestCase):
    def test_rejected_candidate_never_enters_final_list(self):
        rows = [
            {'rank': 1, 'code': '000001', 'score': 9, 'sources': ['A', 'B'],
             'debate_verdict': '否决'},
            {'rank': 2, 'code': '000002', 'score': 2, 'sources': ['A'],
             'debate_verdict': '谨慎'},
        ]
        final = finalize_selection(rows, limit=5)
        self.assertEqual([row['code'] for row in final], ['000002'])

    def test_buy_verdict_can_outweigh_small_rule_score_gap(self):
        rows = [
            {'rank': 1, 'code': '000001', 'score': 3.0, 'sources': ['A', 'B'],
             'debate_verdict': '谨慎', 'debate_confidence': '中'},
            {'rank': 2, 'code': '000002', 'score': 2.5, 'sources': ['A', 'B'],
             'debate_verdict': '买入', 'debate_confidence': '高'},
        ]
        final = finalize_selection(rows, limit=2)
        self.assertEqual(final[0]['code'], '000002')

    def test_chasing_risk_breaks_otherwise_equal_candidates(self):
        rows = [
            {'rank': 1, 'code': '000001', 'score': 3, 'sources': ['A', 'B'],
             'change_pct': 7.0},
            {'rank': 2, 'code': '000002', 'score': 3, 'sources': ['A', 'B'],
             'change_pct': 1.0},
        ]
        final = finalize_selection(rows, limit=2)
        self.assertEqual(final[0]['code'], '000002')
        self.assertGreater(final[1]['chase_penalty'], 0)

    def test_cautious_verdict_is_penalized_against_unreviewed_equal_score(self):
        rows = [
            {'rank': 1, 'code': '000001', 'score': 3, 'sources': ['A', 'B'],
             'debate_verdict': '谨慎', 'debate_confidence': '高'},
            {'rank': 2, 'code': '000002', 'score': 3, 'sources': ['A', 'B']},
        ]
        final = finalize_selection(rows, limit=2)
        self.assertEqual(final[0]['code'], '000002')

    def test_fallback_without_ai_still_returns_five(self):
        rows = [
            {'rank': i, 'code': f'{i:06d}', 'score': 20 - i, 'sources': ['规则']}
            for i in range(1, 9)
        ]
        final = finalize_selection(rows, limit=5)
        self.assertEqual(len(final), 5)
        self.assertTrue(all('待AI' in row['final_reason'] for row in final))
        text = format_final_selection(final)
        self.assertIn('10:05', text)
        self.assertEqual(text.count('优选分'), 5)


if __name__ == '__main__':
    unittest.main()
