from unittest.mock import Mock
import pandas as pd
import pytest
import _bootstrap
from data.sources import eastmoney
from fund.fund_contracts import audit_nav
from fund.fund_returns import portfolio_performance


def source_rows(count=60):
    return [
        {"FSRQ": d.strftime("%Y-%m-%d"), "DWJZ": "1.0", "LJJZ": "1.0", "JZZZL": "0"}
        for d in reversed(pd.bdate_range("2025-01-01", periods=count))
    ]


def test_actual_page_size_and_partial_resume(monkeypatch):
    data = source_rows()
    fail = {"enabled": True}

    def response(url, **kwargs):
        from urllib.parse import parse_qs, urlparse

        page = int(parse_qs(urlparse(url).query)["pageIndex"][0])
        if page == 2 and fail["enabled"]:
            raise TimeoutError()
        return {
            "TotalCount": len(data),
            "Data": {"LSJZList": data[(page - 1) * 20 : page * 20]},
        }

    monkeypatch.setattr(eastmoney.C, "throttle", lambda *a: None)
    monkeypatch.setattr(eastmoney.C, "http_get_json", response)
    first = eastmoney.fund_nav("000001")
    assert len(first) == 20 and audit_nav(first)["status"] == "partial"
    assert first.attrs["pagination_checkpoint"]["next_page"] == 2
    fail["enabled"] = False
    second = eastmoney.fund_nav("000001", first.attrs["pagination_checkpoint"])
    assert len(second) == 60 and audit_nav(second)["status"] == "ready"


def test_duplicate_and_invalid_nav_never_passes_quality(monkeypatch):
    data = source_rows(20)
    monkeypatch.setattr(eastmoney.C, "throttle", lambda *a: None)
    monkeypatch.setattr(
        eastmoney.C,
        "http_get_json",
        lambda *a, **k: {"TotalCount": 100, "Data": {"LSJZList": data}},
    )
    out = eastmoney.fund_nav("000001")
    assert out.attrs["quality"]["status"] == "partial"
    assert "pagination_no_progress" in out.attrs["quality"]["reasons"]
    out.loc[0, "unit_nav"] = -1
    assert "invalid_nav" in audit_nav(out)["reasons"]


def test_partial_source_can_fall_back_without_claiming_independent_upstream(
    monkeypatch,
):
    from data import datahub

    complete = pd.DataFrame(
        {"date": pd.date_range("2026-01-01", periods=30), "unit_nav": [1.0] * 30}
    )
    complete.attrs.update(
        source="akshare",
        upstream="eastmoney",
        quality={"status": "ready", "reasons": []},
    )
    partial = complete.tail(5).copy()
    partial.attrs["quality"] = {"status": "partial", "reasons": ["source_exception"]}
    monkeypatch.setattr(datahub, "_fund_nav_eastmoney", lambda c: partial)
    monkeypatch.setattr(datahub, "_fund_nav_akshare", lambda c: complete)

    def route(cap, sources, empty=None, **kwargs):
        for name, fn in sources:
            candidate = fn()
            if not candidate.empty:
                return candidate
        return empty

    monkeypatch.setattr(datahub, "_route", route)
    result = datahub.fund_nav_history.__wrapped__("000001")
    assert len(result) == 30 and result.attrs["quality"]["status"] == "ready"
    assert result.attrs["upstream"] == "eastmoney"


def test_official_portfolio_performance_cashflow_example():
    # Official PP TTWROR worked example, retrieved 2026-10-15:
    # https://help.portfolio-performance.info/en/concepts/performance/time-weighted/
    marks = [
        {"date": "2021-06-12", "equity": 177.94},
        {"date": "2021-07-01", "equity": 160.26},
        {"date": "2021-08-01", "equity": 264.57, "external_in": 84},
        {"date": "2021-09-01", "equity": 426.82, "external_in": 67},
    ]
    result = portfolio_performance(marks)
    expected = (160.26 / 177.94) * (264.57 / (160.26 + 84)) * (
        426.82 / (264.57 + 67)
    ) - 1
    assert result["time_weighted_return"] == pytest.approx(expected, abs=1e-12)
    assert result["max_drawdown"] == pytest.approx(1 - 160.26 / 177.94)


def _public_samples():
    import json
    from pathlib import Path

    return json.loads(
        (
            Path(__file__).parent / "fixtures/fund_public_snapshots_2026-10-15.json"
        ).read_text()
    )["funds"]


@pytest.mark.parametrize("sample", _public_samples(), ids=lambda x: x["code"])
def test_frozen_public_snapshot_is_not_full_history(sample, monkeypatch):
    from fund.fund_contracts import product

    assert len(sample["rows"]) == 2 and sample["source_total"] > 2
    monkeypatch.setattr(eastmoney.C, "throttle", lambda *a: None)
    monkeypatch.setattr(
        eastmoney.C,
        "http_get_json",
        lambda *a, **k: {
            "TotalCount": sample["source_total"],
            "Data": {"LSJZList": sample["rows"]},
        },
    )
    f = eastmoney.fund_nav(sample["code"])
    assert audit_nav(f)["status"] == "partial"
    profile = product(sample["code"], sample["name"], sample["fund_type"])
    if "货币" in sample["fund_type"]:
        assert profile["category"] == "money"
    else:
        latest, previous = sample["rows"]
        raw = float(latest["DWJZ"]) / float(previous["DWJZ"]) - 1
        disclosed = float(latest["JZZZL"]) / 100
        # The frozen two-date samples reconcile within disclosed % rounding precision.
        assert abs(raw - disclosed) < 0.00006
    if sample["code"] == "000051":
        assert profile["execution"] == "off_exchange"


def test_rendered_fund_notice_archive_delivery_and_repeat_without_real_send(
    tmp_path, monkeypatch
):
    import sqlite3
    from datetime import datetime, timezone
    from cryptography.fernet import Fernet
    from data.research_store import ResearchStore
    from application.message_archive import MessageArchiveService
    from fund.fund_service import render_due
    from notify import notification_router

    store = ResearchStore(
        str(tmp_path / "archive.db"),
        connect_fn=sqlite3.connect,
        is_postgres=False,
        ensure_schema=True,
    )
    archive = MessageArchiveService(
        store=store,
        key=Fernet.generate_key(),
        clock=lambda: datetime(2026, 10, 15, 8, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        notification_router, "_certified_critical_event", lambda *a: True
    )
    monkeypatch.setattr(
        notification_router,
        "archive_action",
        lambda action, body: (
            archive.prepare(**body)
            if action == "prepare"
            else {"started": archive.start(body["delivery_id"])}
            if action == "start"
            else {"recorded": archive.finish(**body)}
        ),
    )
    submitted = []
    monkeypatch.setitem(
        notification_router.CHANNELS,
        "qq",
        lambda title, body: submitted.append(body) or (True, "HTTP 200"),
    )
    body = render_due(
        [
            {
                "code": f"{i:06}",
                "name": "匿名样例基金" * 10,
                "amount": 100,
                "scheduled_date": "2026-10-15",
            }
            for i in range(120)
        ]
    )
    kwargs = {
        "only_channels": ["qq"],
        "source": "fund.plan_due",
        "idempotency_key": "fund-due:2026-10-15-fixture",
        "original_body": body,
        "compact": False,
    }
    first = notification_router.send("report", "基金定投待确认", body, **kwargs)
    second = notification_router.send("report", "基金定投待确认", body, **kwargs)
    assert len(body) > 8000 and submitted == [body]
    assert archive.detail(first.message_id)["original_body"] == body
    assert archive.detail(first.message_id)["deliveries"][0]["suppressed_count"] == 1
    assert second["qq"] == (False, "archive_suppressed")


def test_qdii_plan_never_silently_uses_mainland_calendar(monkeypatch):
    import fund_data
    from fund.fund_service import open_dates_for

    monkeypatch.setattr(fund_data, "fund_type", lambda c: "QDII-混合偏股")
    monkeypatch.setattr(fund_data, "fund_name", lambda c: "匿名境外基金")
    with pytest.raises(ValueError, match="不能直接套用"):
        open_dates_for(
            {"code": "110011", "calendar_id": "CN"}, "2026-10-01", "2026-10-15"
        )
    supplied = ["2026-10-09", "2026-10-12"]
    assert (
        open_dates_for(
            {"code": "110011", "open_dates": supplied}, "2026-10-01", "2026-10-15"
        )
        == supplied
    )


def test_fund_codes_cannot_escape_history_or_holdings_cache(monkeypatch):
    from data import datahub
    import fund_data

    monkeypatch.setattr(
        datahub,
        "_route",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )
    with pytest.raises(ValueError):
        datahub.fund_nav_history.__wrapped__("../../outside")
    with pytest.raises(ValueError):
        fund_data.get_stock_holdings("../../outside", cache_only=True)
