import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from data.sources import cls
from news_flow.premarket_facts import (
    NonTradingReportDate,
    PremarketFactStore,
    build_theme_threads,
    generate_premarket_facts,
    resolve_window,
    tag_events,
)


_CST = ZoneInfo("Asia/Shanghai")


_TEST_SCHEMA = """
CREATE TABLE news_events (
 event_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_event_id TEXT NOT NULL,
 title TEXT NOT NULL, content TEXT NOT NULL, published_at TEXT NOT NULL,
 first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL,
 content_hash TEXT NOT NULL, level TEXT, url TEXT, source_quality TEXT NOT NULL,
 raw_payload TEXT NOT NULL, UNIQUE(source,source_event_id));
CREATE TABLE news_event_revisions (
 revision_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 title TEXT NOT NULL, content TEXT NOT NULL, raw_payload TEXT NOT NULL,
 first_observed_at TEXT NOT NULL, UNIQUE(event_id,content_hash));
CREATE TABLE news_observations (
 observation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_id TEXT NOT NULL,
 observed_at TEXT NOT NULL, bucket TEXT NOT NULL, platform_rank INTEGER,
 platform_heat REAL, url TEXT, UNIQUE(run_id,event_id));
CREATE TABLE news_event_tags (
 run_id TEXT NOT NULL, event_id TEXT NOT NULL, bucket TEXT NOT NULL,
 keep INTEGER NOT NULL, importance INTEGER NOT NULL, information_type TEXT NOT NULL,
 time_role TEXT NOT NULL, market_relevance TEXT NOT NULL, topic_tags TEXT NOT NULL,
 a_share_mapping TEXT NOT NULL, summary TEXT NOT NULL, reason TEXT NOT NULL,
 tagger TEXT NOT NULL, schema_version TEXT NOT NULL, evidence_verified INTEGER NOT NULL,
 tagged_at TEXT NOT NULL, PRIMARY KEY(run_id,event_id));
CREATE TABLE news_theme_threads (
 theme_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
 first_seen_at TEXT NOT NULL, last_catalyst_at TEXT NOT NULL, strength REAL NOT NULL,
 confidence REAL NOT NULL, linked_sector_ids TEXT NOT NULL,
 linked_security_ids TEXT NOT NULL, active_assumptions TEXT NOT NULL,
 invalidation_conditions TEXT NOT NULL, evidence_event_ids TEXT NOT NULL,
 schema_version TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE news_premarket_runs (
 run_id TEXT PRIMARY KEY, report_date TEXT NOT NULL, status TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL, window_start TEXT NOT NULL,
 background_end TEXT NOT NULL, window_end TEXT NOT NULL, input_count INTEGER NOT NULL,
 deduplicated_count INTEGER NOT NULL, inserted_count INTEGER NOT NULL,
 revised_count INTEGER NOT NULL, tagged_count INTEGER NOT NULL,
 local_fallback_count INTEGER NOT NULL, theme_count INTEGER NOT NULL,
 evidence_coverage REAL NOT NULL, stage_status TEXT NOT NULL, warnings TEXT NOT NULL,
 data_notes TEXT NOT NULL, brief_payload TEXT NOT NULL, schema_version TEXT NOT NULL);
"""


class PremarketWindowTests(unittest.TestCase):
    def test_a_share_double_window(self):
        window = resolve_window("2026-08-24", ["2026-08-20", "2026-08-21", "2026-08-24"])
        self.assertEqual(window.previous_trade_date, "2026-08-21")
        self.assertEqual(window.start.strftime("%Y-%m-%d %H:%M"), "2026-08-21 08:30")
        self.assertEqual(window.background_end.strftime("%Y-%m-%d %H:%M"), "2026-08-21 15:00")
        self.assertEqual(window.end.strftime("%Y-%m-%d %H:%M"), "2026-08-24 08:30")
        self.assertEqual(window.bucket_for("2026-08-21T14:59:59+08:00"), "background")
        self.assertEqual(window.bucket_for("2026-08-21T15:00:00+08:00"), "main")

    def test_unconfirmed_report_date_is_rejected(self):
        with self.assertRaises(NonTradingReportDate):
            resolve_window("2026-08-23", ["2026-08-21", "2026-08-24"])

    def test_deployment_schema_contains_the_whole_fact_slice(self):
        root = Path(__file__).resolve().parents[1]
        schema = (root / "scripts" / "init_postgres.sql").read_text(encoding="utf-8")
        for table in (
            "news_events", "news_event_revisions", "news_observations",
            "news_event_tags", "news_theme_threads", "news_premarket_runs",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", schema)
        self.assertIn("8-premarket-facts", schema)


class ClsRedSourceTests(unittest.TestCase):
    def test_red_source_keeps_stable_id_and_filters_window(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "errno": 0,
                    "data": {"roll_data": [
                        {"id": 20, "ctime": 200, "level": "A",
                         "content": "【算力政策】有关部门发布支持意见"},
                        {"id": 10, "ctime": 90, "level": "B", "content": "窗口外"},
                    ]},
                }

        class Session:
            def __init__(self):
                self.params = None

            def get(self, _url, **kwargs):
                self.params = kwargs["params"]
                return Response()

        session = Session()
        with mock.patch.object(cls.C, "requests_session", return_value=session):
            rows = cls.red_telegraphs(100, 250, max_pages=2)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_event_id"], "20")
        self.assertEqual(rows[0]["title"], "算力政策")
        self.assertEqual(rows[0]["source_quality"], "primary_fact")
        self.assertEqual(session.params["category"], "red")
        self.assertEqual(len(session.params["sign"]), 32)


class PremarketFactPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "facts.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_TEST_SCHEMA)
        conn.close()
        self.store = PremarketFactStore(lambda: sqlite3.connect(self.db_path))
        self.trade_days = ["2026-08-20", "2026-08-21", "2026-08-24"]
        self.now = datetime(2026, 8, 24, 9, 0, tzinfo=_CST)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _events():
        return [
            {
                "source": "cls_red", "source_event_id": "1001", "title": "算力支持政策发布",
                "content": "国务院发布人工智能数据中心支持意见", "level": "A",
                "published_at": "2026-08-21T16:00:00+08:00", "url": "https://example.com/1001",
                "source_quality": "primary_fact", "raw_payload": {"id": 1001},
            },
            {
                "source": "cls_red", "source_event_id": "1002", "title": "芯片产业新进展",
                "content": "半导体设备取得重大突破", "level": "B",
                "published_at": "2026-08-24T07:00:00+08:00", "url": "https://example.com/1002",
                "source_quality": "primary_fact", "raw_payload": {"id": 1002},
            },
        ]

    def test_source_id_deduplicates_body_but_keeps_observations_and_revisions(self):
        fetcher = lambda *_args: self._events()
        first = generate_premarket_facts(
            "2026-08-24", trade_days=self.trade_days, fetcher=fetcher,
            store=self.store, now=self.now,
        )
        second = generate_premarket_facts(
            "2026-08-24", trade_days=self.trade_days, fetcher=fetcher,
            store=self.store, now=self.now,
        )

        conn = sqlite3.connect(self.db_path)
        counts = {
            "events": conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0],
            "observations": conn.execute("SELECT COUNT(*) FROM news_observations").fetchone()[0],
            "revisions": conn.execute("SELECT COUNT(*) FROM news_event_revisions").fetchone()[0],
        }
        conn.close()
        self.assertEqual(counts, {"events": 2, "observations": 4, "revisions": 2})
        self.assertEqual(first["metrics"]["inserted_count"], 2)
        self.assertEqual(second["metrics"]["inserted_count"], 0)
        self.assertEqual(first["metrics"]["evidence_coverage"], 1.0)
        self.assertTrue(all(row["evidence_verified"] for row in first["brief"]["facts"]))

        changed = self._events()
        changed[0] = {**changed[0], "content": "国务院发布人工智能数据中心支持意见（更新）"}
        third = generate_premarket_facts(
            "2026-08-24", trade_days=self.trade_days, fetcher=lambda *_args: changed,
            store=self.store, now=self.now,
        )
        conn = sqlite3.connect(self.db_path)
        revision_count = conn.execute("SELECT COUNT(*) FROM news_event_revisions").fetchone()[0]
        conn.close()
        self.assertEqual(third["metrics"]["revised_count"], 1)
        self.assertEqual(revision_count, 3)

    def test_tags_and_threads_are_reference_only_and_have_evidence(self):
        window = resolve_window("2026-08-24", self.trade_days)
        events = []
        for index, raw in enumerate(self._events(), 1):
            events.append({
                **raw, "event_id": f"event-{index}",
                "first_observed_at": self.now.isoformat(),
            })
        tags = tag_events(events, window, {})
        threads = build_theme_threads(tags, {}, self.now.isoformat())

        self.assertEqual(tags[0]["information_type"], "policy_regulation")
        self.assertEqual(tags[0]["time_role"], "new_catalyst")
        self.assertEqual(tags[1]["time_role"], "preopen_increment")
        self.assertNotIn("security_code", json.dumps(tags, ensure_ascii=False))
        self.assertTrue(threads)
        self.assertTrue(all(thread["linked_security_ids"] == [] for thread in threads))
        self.assertTrue(all(thread["evidence_event_ids"] for thread in threads))

    def test_fetch_failure_uses_cached_events_and_exposes_degradation(self):
        generate_premarket_facts(
            "2026-08-24", trade_days=self.trade_days, fetcher=lambda *_args: self._events(),
            store=self.store, now=self.now,
        )

        def failed_fetch(*_args):
            raise TimeoutError("source timeout")

        result = generate_premarket_facts(
            "2026-08-24", trade_days=self.trade_days, fetcher=failed_fetch,
            store=self.store, now=self.now,
        )
        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["stage_status"]["source_fetch"], "degraded")
        self.assertEqual(len(result["brief"]["facts"]), 2)
        self.assertNotIn("source timeout", " ".join(result["warnings"]))


if __name__ == "__main__":
    unittest.main()
