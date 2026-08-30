import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import pandas as pd

import _bootstrap  # noqa: F401
from analysis import factor_eval
from analysis.selection_feature_catalog import (
    DATA_QUALITY_WEIGHT,
    FUNDAMENTAL_FAMILY_WEIGHTS,
    INDUSTRY_FEATURE_WEIGHTS,
    TECHNICAL_FEATURES,
    catalog_manifest,
)
from core.action_decision import resolve_action
from notify.notification_service import NotificationService
from rag import store as rag_store


class ResearchFeatureContractTests(unittest.TestCase):
    def test_catalog_preserves_formal_score_budget(self):
        self.assertEqual(sum(item.weight for item in TECHNICAL_FEATURES), 30)
        self.assertEqual(sum(FUNDAMENTAL_FAMILY_WEIGHTS.values()), 40)
        self.assertEqual(sum(INDUSTRY_FEATURE_WEIGHTS.values()), 15)
        self.assertEqual(DATA_QUALITY_WEIGHT, 15)
        self.assertFalse(catalog_manifest()["mutable_by_research"])

    def test_default_factor_universe_uses_lifecycle_membership(self):
        frame = pd.DataFrame({
            "symbol": [f"{600000 + index:06d}" for index in range(25)]
        })
        frame.attrs.update({
            "lifecycle_complete": True,
            "membership_basis": "security_lifecycle_backfill",
            "snapshot_date": "2026-08-21",
        })
        with patch("data.research_store.ResearchStore.load_lifecycle_universe",
                   return_value=frame):
            universe, evidence = factor_eval._default_research_universe("1y", limit=20)
        self.assertEqual(len(universe), 20)
        self.assertFalse(evidence["exploratory"])
        self.assertEqual(evidence["basis"], "security_lifecycle_backfill")


class ActionResolutionTests(unittest.TestCase):
    def test_hard_risk_beats_llm_add_advice(self):
        result = resolve_action([
            {"source": "hard_risk", "action": "sell", "reason": "已触发止损"},
            {"source": "llm", "action": "add", "reason": "模型看涨"},
        ])
        self.assertEqual(result["action"], "sell")
        self.assertEqual(result["action_text"], "卖出")
        self.assertEqual(result["source"], "hard_risk")

    def test_external_reference_cannot_create_trade_action(self):
        result = resolve_action([
            {"source": "external_reference", "action": "add", "reason": "问财命中"},
        ])
        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["action_text"], "不动")

    def test_equal_priority_prefers_risk_reduction(self):
        result = resolve_action([
            {"source": "formal_signal", "action": "add", "reason": "趋势向上"},
            {"source": "formal_signal", "action": "reduce", "reason": "波动过大"},
        ])
        self.assertEqual(result["action"], "reduce")


class NotificationRedactionTests(unittest.TestCase):
    def _service(self):
        service = NotificationService.__new__(NotificationService)
        service.config = {
            "webhook_enabled": True,
            "webhook_type": "dingtalk",
            "webhook_url": "https://secret.example.invalid/token-sensitive",
            "webhook_keyword": "测试",
        }
        return service

    def test_config_status_does_not_return_webhook_url(self):
        status = self._service().get_webhook_config_status()
        self.assertNotIn("webhook_url", status)
        self.assertTrue(status["configured"])

    @patch("requests.post")
    def test_webhook_error_log_omits_url_and_upstream_body(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "errcode": 1,
            "errmsg": "token-sensitive portfolio=123456",
        }
        post.return_value = response
        output = io.StringIO()
        notification = {
            "id": 1, "symbol": "600000", "name": "样本", "type": "减仓",
            "message": "持仓敏感正文", "triggered_at": "2026-08-30 10:00:00",
        }
        with redirect_stdout(output):
            success = self._service()._send_dingtalk_webhook(notification)
        self.assertFalse(success)
        logged = output.getvalue()
        self.assertNotIn("token-sensitive", logged)
        self.assertNotIn("portfolio=", logged)
        self.assertNotIn("600000", logged)


class PostgresRuntimeCleanupTests(unittest.TestCase):
    @patch("psycopg2.connect")
    def test_rag_uses_shared_postgres_settings_without_legacy_switch(self, connect):
        expected = object()
        connect.return_value = expected
        rag_store._down_until = 0.0
        self.assertIs(rag_store._conn(), expected)
        connect.assert_called_once_with(**rag_store.PG_CONFIG)


if __name__ == "__main__":
    unittest.main()
