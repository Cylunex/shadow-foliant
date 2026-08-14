import inspect
import unittest

from jobs import jobs_hub
from jobs.automation_config import REGISTRY
from jobs.schedule_policy import (
    EVENING_MAX_RUNTIME_MINUTES,
    EVENING_TIMES,
    WEEKEND_LLM_BLACKOUTS,
    WEEKEND_LLM_JOBS,
    WEEKEND_TIMES,
    interval_overlaps,
    minute_of_day,
    weekend_llm_allowed,
)


class SchedulePolicyTests(unittest.TestCase):
    def test_weekend_llm_jobs_do_not_overlap_blackout_windows(self):
        durations = {'mx_weekend_outlook': 10, 'weekend_portfolio': 90}
        for name in WEEKEND_LLM_JOBS:
            start = minute_of_day(WEEKEND_TIMES[name][1])
            for blocked_start, blocked_end in WEEKEND_LLM_BLACKOUTS:
                self.assertFalse(
                    interval_overlaps(start, durations[name], blocked_start, blocked_end),
                    f'{name} overlaps {blocked_start}-{blocked_end}',
                )

    def test_evening_jobs_finish_by_2330_under_timeout_budget(self):
        starts = dict(EVENING_TIMES)
        starts['weekly_db_cleanup'] = WEEKEND_TIMES['weekly_db_cleanup'][1]
        for name, hhmm in starts.items():
            finish = minute_of_day(hhmm) + EVENING_MAX_RUNTIME_MINUTES[name]
            self.assertLessEqual(finish, 23 * 60 + 30, name)
            self.assertLess(finish, 24 * 60, name)

    def test_legacy_llm_guard_only_blocks_weekend_windows(self):
        from datetime import datetime
        self.assertTrue(weekend_llm_allowed(datetime(2026, 8, 14, 10, 0), 90))  # 周五
        self.assertFalse(weekend_llm_allowed(datetime(2026, 8, 15, 10, 0), 15))  # 周六禁用段
        self.assertFalse(weekend_llm_allowed(datetime(2026, 8, 16, 13, 30), 90))  # 跨入14点
        self.assertTrue(weekend_llm_allowed(datetime(2026, 8, 16, 12, 5), 90))
        self.assertTrue(weekend_llm_allowed(datetime(2026, 8, 16, 18, 0), 30))

    def test_registry_and_scheduler_share_policy_times(self):
        self.assertEqual(REGISTRY['mx_weekend_outlook']['schedule'], '周日 08:00')
        self.assertEqual(REGISTRY['weekend_portfolio']['schedule'], '周日 12:05')
        self.assertEqual(REGISTRY['weekly_db_cleanup']['schedule'], '周日 23:15')
        self.assertEqual(REGISTRY['pg_backup']['schedule'], '23:00 每日')
        self.assertTrue(REGISTRY['rag_ingest']['schedule'].startswith('20:15 每日'))

        source = inspect.getsource(jobs_hub.register_default_jobs)
        for name in ('mx_weekend_outlook', 'weekend_portfolio', 'weekly_db_cleanup',
                     'wf_weekly_backtest', 'ai_eval_weekly'):
            self.assertIn(f"WEEKEND_TIMES['{name}']", source)
        for name in ('rag_ingest', 'fund_evening', 'daily_pnl_snapshot', 'pg_backup'):
            self.assertIn(f"EVENING_TIMES['{name}']", source)


if __name__ == '__main__':
    unittest.main()
