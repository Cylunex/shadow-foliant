"""Protocol failures must fall through without fabricating classification or news."""
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from analysis.market_structure import build_market_structure
from data import datahub
from data.sources import cls, eastmoney


@pytest.mark.parametrize("empty", [
    {"industry": [], "concept": [], "region": [], "concept_tags": []},
    {"top": [], "bottom": [], "total": 0},
    {"success": False, "error": "source unavailable"},
    pd.DataFrame(),
])
def test_route_empty_payload_records_failure_and_uses_next_source(empty):
    with patch.object(datahub, "_STATS", {}), \
            patch("data.provider_governor.operational_priority", return_value=1), \
            patch("data.provider_governor.provider_slot", return_value=nullcontext()):
        result = datahub._route("recovery_test", [
            ("first", lambda: empty), ("second", lambda: ["usable"]),
        ])
        assert result == ["usable"]
        stats = datahub.source_stats()
        assert stats["recovery_test:first"]["fail"] == 1
        assert stats["recovery_test:first"]["ok"] == 0
        assert stats["recovery_test:second"]["ok"] == 1


def _response(payload):
    response = Mock()
    response.json.return_value = payload
    return response


def test_cls_telegraph_uses_signed_all_category_without_changing_red_default():
    response = _response({"errno": 0, "data": {"roll_data": [
        None, {"title": "<b>快讯</b>", "content": "消息&nbsp;正文", "ctime": 123}, {},
    ]}})
    with patch.object(cls.C, "requests_session") as session, patch.object(cls.time, "time", return_value=123):
        session.return_value.get.return_value = response
        result = cls.telegraph(200)
        call = session.return_value.get.call_args
    assert call.args[0].endswith("/v1/roll/get_roll_list")
    params = call.kwargs["params"]
    assert params["category"] == "" and params["rn"] == 100
    assert params["sign"] == cls._red_signature({k: v for k, v in params.items() if k != "sign"})
    assert cls._red_params(123, 20)["category"] == "red"
    assert result == [{"title": "快讯", "content": "消息 正文", "time": 123}]
    response.raise_for_status.assert_called_once()


@pytest.mark.parametrize("payload", [None, {"errno": 1}, {"errno": 0, "data": "bad"},
                                    {"errno": 0, "data": {"roll_data": "bad"}}])
def test_cls_invalid_response_is_empty(payload):
    with patch.object(cls.C, "requests_session") as session:
        session.return_value.get.return_value = _response(payload)
        assert cls.telegraph() == []


@pytest.mark.parametrize("as_dict", [True, False])
def test_eastmoney_mixed_boards_remain_unclassified(as_dict):
    rows = [{"f12": "BK0001", "f14": "示例板块"}, "bad", {"f12": "BK0001", "f14": "重复"},
            {"f12": "600519", "f14": "非板块"}, {"f12": "BK0002", "f14": None}]
    payload = {"rc": 0, "data": {"diff": dict(enumerate(rows)) if as_dict else rows}}
    with patch.object(eastmoney.C, "requests_session") as session:
        session.return_value.get.return_value = _response(payload)
        result = eastmoney.related_boards("600519")
    assert result["unclassified_boards"] == [{"code": "BK0001", "name": "示例板块"}]
    assert result["classification_status"] == "mixed_unclassified"
    assert all(not result[key] for key in ("industry", "concept", "region", "concept_tags"))
    structure = build_market_structure({"sentiment": {
        "concept_blocks": result, "hot_themes": [{"theme": "示例板块"}],
        "hot_stocks": [{"code": "600519", "theme": "示例板块"}],
    }}, "600519")
    assert structure["stock_role"] == "unknown"
    assert structure["primary_theme"] is None
    assert structure["related_unclassified_boards"] == ["示例板块"]
    assert any("未区分" in reason for reason in structure["limitations"])


@pytest.mark.parametrize("payload", [{"rc": 1}, {"rc": 0, "data": {"diff": {}}},
                                    {"rc": 0, "data": {"diff": [None, {}]}}])
def test_eastmoney_no_data_does_not_return_truthy_metadata(payload):
    with patch.object(eastmoney.C, "requests_session") as session:
        session.return_value.get.return_value = _response(payload)
        assert eastmoney.related_boards("600519") == {}


def test_classified_blocks_take_precedence_and_empty_baidu_uses_fallback():
    # Bypass cache only: exercise real routing with isolated provider controls.
    function = getattr(datahub.concept_blocks, "__wrapped__", datahub.concept_blocks)
    classified = {"concept": ["AI"]}
    mixed = {"unclassified_boards": [{"name": "示例板块"}]}
    with patch.object(datahub, "_STATS", {}), \
            patch("data.provider_governor.provider_slot", side_effect=lambda *a, **kw: nullcontext()), \
            patch("data.sources.baidu.concept_blocks", return_value=classified) as baidu, \
            patch.object(eastmoney, "related_boards", return_value=mixed) as fallback:
        assert function("600519") == classified
        fallback.assert_not_called()
        baidu.return_value = {"industry": [], "concept": [], "region": [], "concept_tags": []}
        assert function("600519") == mixed
        fallback.assert_called_once_with("600519")
