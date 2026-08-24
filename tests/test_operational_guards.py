import os
import importlib
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import _bootstrap  # noqa: F401  让独立运行本文件时也注册功能子目录
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
    @patch('portfolio_policy.latest_market_add_signal', return_value={'action': 'strong_buy'})
    def test_high_position_allows_buy_only_in_must_add_window(self, _signal):
        guarded = portfolio_policy.guard('add', source_type='analysis')
        self.assertFalse(guarded['blocked'])
        self.assertEqual(guarded['action'], 'add')

    @patch.dict(os.environ, {'PORTFOLIO_POSITION_MODE': 'high'})
    @patch('portfolio_policy.latest_market_add_signal', return_value={'action': 'buy'})
    def test_high_position_still_blocks_only_moderate_buy(self, _signal):
        guarded = portfolio_policy.guard('buy', source_type='selection')
        self.assertTrue(guarded['blocked'])

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


class LegacyAutostartTest(unittest.TestCase):
    def test_import_has_no_process_side_effect(self):
        sys.modules.pop("autostart", None)
        with patch.dict(os.environ, {"AUTOSTART_ENABLED": "true"}):
            module = importlib.import_module("autostart")
        self.assertFalse(module._STARTED)


class RemovedSqliteModuleTest(unittest.TestCase):
    def test_runtime_code_no_longer_imports_deleted_database_module(self):
        root = os.path.dirname(os.path.dirname(__file__))
        for relative in ('core/batch_analyze.py', 'jobs/jobs_hub.py'):
            with open(os.path.join(root, relative), encoding='utf-8') as source:
                self.assertNotIn('from database import db', source.read())


class ControlledDeployTest(unittest.TestCase):
    def test_release_keeps_supervisor_entrypoint_and_shared_environment(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, 'scripts/deploy.sh'), encoding='utf-8') as source:
            script = source.read()
        self.assertIn('ln -sfn venv "$release_dir/venv2"', script)
        self.assertIn('FOLIANT_SHARED_ENV is required for release activation', script)
        self.assertIn('ln -sfn "$shared_env" "$release_dir/.env"', script)


class StrategyDeploymentQualityTest(unittest.TestCase):
    def test_holdout_quality_changes_deployment_order(self):
        from analysis.strategy_genome import deployment_quality_score
        overfit = deployment_quality_score(90, 20, -2, 5)
        robust = deployment_quality_score(75, 60, 2, 12)
        self.assertGreater(robust, overfit)

    def test_composed_signature_deduplicates_parameter_clones(self):
        from analysis.strategy_genome import composed_signature
        a = [{'b': 'ma_rising', 'p': {'period': 20}}, {'b': 'rsi_below', 'p': {'x': 24}}]
        b = [{'b': 'rsi_below', 'p': {'x': 30}}, {'b': 'ma_rising', 'p': {'period': 45}}]
        self.assertEqual(composed_signature(a), composed_signature(b))

    def test_runner_uses_deployment_quality_as_match_score(self):
        from selection import instock_strategy_runner as runner

        def always_match(code_name, frame, date=None):
            return True

        frame = pd.DataFrame({
            'Date': pd.date_range('2026-01-01', periods=3),
            'Open': [10, 10.1, 10.2], 'High': [10.2, 10.3, 10.4],
            'Low': [9.9, 10.0, 10.1], 'Close': [10.1, 10.2, 10.3],
            'Volume': [100, 110, 120],
        })
        strategies = {'dummy': {'cn': '测试策略', 'category': '测试',
                                'min_days': 1, 'func': always_match}}
        live = {'base': {'dummy': {}},
                'base_meta': {'dummy': {'deployment_score': 80, 'generation': 2}},
                'composed': []}
        with patch.object(runner, 'STRATEGIES', strategies), \
                patch.object(runner, '_live_genome_set', return_value=live):
            result = runner.run_one('600000', frame, evolved=True)
        self.assertEqual(result['matched_count'], 1)
        self.assertEqual(result['match_score'], 0.8)
        self.assertEqual(result['matched'][0]['generation'], 2)


if __name__ == '__main__':
    unittest.main()
