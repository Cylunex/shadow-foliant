import contextlib
from datetime import date
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import _bootstrap  # noqa: F401
import datahub
from data.source_contracts import contracts
from data.sources._mairui_compatible import MairuiCompatibleSource
from data.sources._path_token_api import PathTokenApi
from analysis.announcement_scan import _recent_records


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, timeout):
        self.calls.append((url, timeout))
        return self.responses.pop(0)


class _Api:
    provider = "mairui"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, capability, parts, params=None):
        self.calls.append((capability, parts, params or {}))
        return self.responses.pop(0) if self.responses else []

    def available(self):
        return True

    def budget_status(self):
        return {"available": True, "used": 0, "limit": 450}

    def reset_for_tests(self):
        pass


class PathTokenTransportTest(unittest.TestCase):
    def _client(self):
        return PathTokenApi(
            provider="mairui", token_env="MAIRUI_LICENCE",
            enabled_env="MAIRUI_ENABLED", base_url_env="MAIRUI_BASE_URL",
            default_base_url="https://api.example.com",
            state_dir_env="MAIRUI_STATE_DIR",
            budget_env="MAIRUI_DAILY_REQUEST_BUDGET",
        )

    def test_budget_is_shared_across_capabilities_and_stops_before_http(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "MAIRUI_LICENCE": "secret-value",
            "MAIRUI_STATE_DIR": tmp,
            "MAIRUI_DAILY_REQUEST_BUDGET": "2",
        }, clear=False):
            client = self._client()
            session = _Session([_Response([]), _Response([])])
            client._session = session
            with patch("data.sources._path_token_api.source_call",
                       return_value=contextlib.nullcontext()):
                client.get("realtime", ["hsrl", "ssjy_more"])
                client.get("announcements", ["hsstock", "announcement", "600000"])
                second_client = self._client()
                second_session = _Session([_Response([])])
                second_client._session = second_session
                self.assertEqual(
                    second_client.get(
                        "interactive_qa", ["hsstock", "interactiveqa", "600000"]
                    ),
                    [],
                )
            self.assertEqual(len(session.calls), 2)
            self.assertEqual(second_session.calls, [])
            status = client.budget_status()
            self.assertEqual(status["used"], 2)
            self.assertEqual(status["capabilities"], {
                "realtime": 1, "announcements": 1,
            })
            self.assertNotIn("secret-value", str(status))

    def test_configured_budget_cannot_exceed_provider_plan(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "MAIRUI_STATE_DIR": tmp,
            "MAIRUI_DAILY_REQUEST_BUDGET": "999999",
        }, clear=False):
            self.assertEqual(self._client().budget_status()["limit"], 500)

    def test_non_https_base_url_is_rejected_without_sending_token(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "MAIRUI_LICENCE": "secret-value",
            "MAIRUI_STATE_DIR": tmp,
            "MAIRUI_BASE_URL": "http://api.example.com",
        }, clear=False):
            client = self._client()
            session = _Session([])
            client._session = session
            self.assertEqual(client.get("realtime", ["hsrl", "ssjy_more"]), [])
            self.assertEqual(session.calls, [])


class CompatibleNormalizationTest(unittest.TestCase):
    def test_quotes_are_batched_by_twenty_and_normalized(self):
        rows1 = [{
            "dm": "600000", "mc": "浦发银行", "p": 10.2, "yc": 10.0,
            "o": 10.0, "h": 10.3, "l": 9.9, "cje": 120000,
            "pc": 2.0, "pe": 6.0, "pb_ratio": 0.7, "sz": 2e11,
        }]
        rows2 = [{"dm": "600020", "p": 8.0, "yc": 8.0}]
        api = _Api([rows1, rows2])
        source = MairuiCompatibleSource(api, pro_env="MAIRUI_QUANT_PRO_ENABLED")
        result = source.quotes([f"{600000 + i:06d}" for i in range(21)])
        self.assertEqual(len(api.calls), 2)
        self.assertEqual(result["600000"]["price"], 10.2)
        self.assertEqual(result["600000"]["amount_wan"], 12.0)
        self.assertEqual(result["600000"]["mcap_yi"], 2000.0)
        self.assertEqual(result["600000"]["source"], "mairui")

    def test_kline_infers_lots_and_preserves_intraday_timestamp(self):
        rows = [
            {"t": f"2026-08-28 09:{35 + i:02d}:00", "o": 10, "h": 10.2,
             "l": 9.9, "c": 10.1, "v": 100 + i, "a": (100 + i) * 10.1 * 100}
            for i in range(3)
        ]
        source = MairuiCompatibleSource(_Api([rows]), pro_env="MAIRUI_QUANT_PRO_ENABLED")
        frame = source.kline("600000", interval="5m", count=3)
        self.assertEqual(frame["volume"].tolist(), [10000.0, 10100.0, 10200.0])
        self.assertEqual(str(frame.iloc[0]["date"]), "2026-08-28 09:35:00")

    def test_one_minute_requires_explicit_pro_entitlement(self):
        api = _Api([[{"t": "2026-08-28 09:31:00"}]])
        source = MairuiCompatibleSource(api, pro_env="MAIRUI_QUANT_PRO_ENABLED")
        with patch.dict(os.environ, {"MAIRUI_QUANT_PRO_ENABLED": "false"}):
            self.assertTrue(source.kline("600000", interval="1m").empty)
        self.assertEqual(api.calls, [])

    def test_flow_and_reference_events_are_normalized(self):
        api = _Api([
            [{"t": "2026-08-28", "zmbtdcje": 100, "zmstdcje": 20,
              "zmbddcje": 50, "zmsddcje": 10, "zmbzdcje": 30,
              "zmszdcje": 5, "zmbxdcje": 10, "zmsxdcje": 3}],
            [{"t": "2026-08-28", "zt": "重大合同公告", "lx": "合同", "nr": "正文"}],
            [{"t": "2026-08-28", "id": 1, "q": "产能如何？", "a": "按计划推进"}],
            [{"t": "2026-08-28 15:00:00", "bu": 1, "dr": 1}],
            [{"t": "2026-08-28 09:25:00", "ov": 1000, "bp": 1.2}],
        ])
        source = MairuiCompatibleSource(api, pro_env="MAIRUI_QUANT_PRO_ENABLED")
        flow = source.capital_flow("600000", 10)[0]
        self.assertEqual(flow["super_net"], 80.0)
        self.assertEqual(flow["large_net"], 40.0)
        self.assertEqual(flow["main_net"], 120.0)
        announcement = source.announcements("600000")[0]
        self.assertFalse(announcement["official"])
        self.assertEqual(source.interactive_qa("600000")[0]["reference_only"], True)
        self.assertEqual(source.limit_performance("600000")[0]["break_count"], 1)
        self.assertEqual(
            source.auction_performance("600000")[0]["open_auction_volume_lots"],
            1000,
        )
        self.assertEqual(
            api.calls[1],
            ("announcements", ["hsstock", "announcement", "600000"], {"lt": 30}),
        )
        self.assertEqual(
            api.calls[3][1], ["hsstock", "lup", "limit", "600000"]
        )


class DatahubRoutingTest(unittest.TestCase):
    def setUp(self):
        datahub._STATS.clear()

    def test_mairui_quote_is_only_used_after_existing_sources_are_empty(self):
        class Adapter:
            get_quotes = staticmethod(lambda _codes: {})
            get_quotes_eastmoney = staticmethod(lambda _codes: {})
            get_quotes_sina = staticmethod(lambda _codes: {})

        expected = {"600000": {"name": "浦发银行", "price": 10.0}}
        with patch.object(datahub, "_adapter", return_value=Adapter()), \
                patch.object(datahub, "_zzshare_available", return_value=False), \
                patch.object(datahub, "_eltdx_available", return_value=False), \
                patch.object(datahub, "_tdx_python_available", return_value=False), \
                patch.object(datahub, "_easy_tdx_available", return_value=False), \
                patch.object(datahub, "_mairui_available", return_value=True), \
                patch.object(datahub, "_moma_available", return_value=False), \
                patch.object(datahub, "_quotes_mairui", return_value=expected), \
                patch.object(datahub, "_name_remember"):
            actual = datahub.quotes.__wrapped__(["600000"])
        self.assertEqual(actual, expected)

    def test_contracts_publish_bounded_family_capabilities(self):
        matrix = contracts()
        for provider in ("mairui", "moma"):
            self.assertEqual(matrix[provider]["realtime"]["hard_max_rows"], 20)
            self.assertEqual(matrix[provider]["capital_flow"]["daily_request_limit"], 500)
        self.assertEqual(matrix["mairui"]["interactive_qa"]["max_concurrency"], 1)
        self.assertNotIn("interactive_qa", matrix["moma"])


class AnnouncementSourceTest(unittest.TestCase):
    def test_aggregated_fallback_is_not_relabelled_as_official(self):
        rows = [{
            "date": date.today().isoformat(),
            "title": "测试公告",
            "source": "mairui",
            "official": False,
            "url": "",
        }]
        with patch("datahub.announcements", return_value=rows):
            actual = _recent_records("600000", days=1)
        self.assertEqual(actual[0]["source"], "mairui")
        self.assertFalse(actual[0]["official"])


if __name__ == "__main__":
    unittest.main()
