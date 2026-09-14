from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import pytest

from data.sources import fuyao_aicubes as fuyao


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@contextmanager
def no_gate(*_args, **_kwargs):
    yield


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("FUYAO_AICUBES_API_KEY", "test-only-secret")
    monkeypatch.setenv("FUYAO_AICUBES_ENABLED", "true")
    monkeypatch.setenv("FUYAO_AICUBES_SNAPSHOT_CACHE_TTL_SECONDS", "0")
    monkeypatch.setattr(fuyao, "source_call", no_gate)
    monkeypatch.setattr(fuyao.time, "sleep", lambda _seconds: None)
    fuyao._reset_for_tests()
    yield
    fuyao._reset_for_tests()


def stamp(value: str) -> int:
    return int(pd.Timestamp(value, tz="Asia/Shanghai").timestamp() * 1000)


def test_quote_batch_mapping_order_and_metadata(monkeypatch):
    session = Session([Response({"code": 0, "request_id": "request_123", "data": {
        "timestamp": stamp("2026-09-14 15:00:00"), "item": [
            {"thscode": "000001.SZ", "ticker": "000001", "last_price": 10,
             "prev_price": 9, "open_price": 9.5, "high_price": 10.5,
             "low_price": 9.2, "price_change": 1, "price_change_ratio_pct": 11.11,
             "volume": 100, "turnover": 12345},
            {"thscode": "600000.SH", "ticker": "600000", "last_price": 8,
             "prev_price": 8, "open_price": 8, "high_price": 8.2,
             "low_price": 7.9, "price_change": 0, "price_change_ratio_pct": 0,
             "volume": 200, "turnover": 23456},
        ]}})])
    monkeypatch.setattr(fuyao, "_SESSION", session)
    result = fuyao.get_quotes(["000001", "600000"], use_cache=False)
    assert list(result) == ["000001", "600000"]
    assert result["000001"]["amount_wan"] == 1.2345
    assert result["000001"]["provider"] == "fuyao_aicubes"
    assert result["000001"]["request_id"] == "request_123"
    assert result["000001"]["adjustment"] == "raw"
    assert session.calls[0][1]["headers"]["X-api-key"] == "test-only-secret"
    assert "test-only-secret" not in repr(fuyao.capability_status())


def test_batch_is_bounded_to_one_hundred_and_keeps_requested_order(monkeypatch):
    items = []
    symbols = [f"{value:06d}" for value in range(1, 102)]
    for page in (symbols[:100], symbols[100:]):
        items.append(Response({"code": 0, "request_id": "request_456", "data": {
            "timestamp": stamp("2026-09-14 14:00:00"), "item": [
                {"thscode": fuyao.to_thscode(code), "last_price": 1,
                 "prev_price": 1, "high_price": 1, "low_price": 1}
                for code in reversed(page)
            ]}}))
    session = Session(items)
    monkeypatch.setattr(fuyao, "_SESSION", session)
    result = fuyao.get_quotes(symbols, use_cache=False)
    assert list(result) == symbols
    assert len(session.calls) == 2


@pytest.mark.parametrize("code,error_type", [
    (2001, fuyao.FuyaoAuthenticationError),
    (2003, fuyao.FuyaoPermissionError),
])
def test_business_permission_errors_are_classified(monkeypatch, code, error_type):
    monkeypatch.setattr(fuyao, "_SESSION", Session([
        Response({"code": code, "message": "must not leak", "request_id": "request_789"})
    ]))
    with pytest.raises(error_type) as caught:
        fuyao._request("snapshot", "/test", use_cache=False)
    assert "must not leak" not in str(caught.value)


@pytest.mark.parametrize("code", [3001, 3002, 3004])
def test_empty_business_codes_degrade_without_exception(monkeypatch, code):
    monkeypatch.setattr(fuyao, "_SESSION", Session([
        Response({"code": code, "request_id": "request_empty"})
    ]))
    value = fuyao._request("snapshot", "/test", use_cache=False)
    assert value["data"] is None
    assert fuyao.capability_status()["capabilities"]["snapshot"]["status"] == "degraded"


def test_http_and_business_rate_limits_retry_with_bound(monkeypatch):
    session = Session([
        Response({}, status=429, headers={"Retry-After": "1"}),
        Response({"code": 4001, "request_id": "request_rate"}),
        Response({"code": 0, "request_id": "request_ok", "data": {"item": []}}),
    ])
    monkeypatch.setattr(fuyao, "_SESSION", session)
    assert fuyao._request("snapshot", "/test", use_cache=False)["code"] == 0
    assert len(session.calls) == 3


def test_transport_timeout_retries_and_surfaces_safe_error(monkeypatch):
    Timeout = type("Timeout", (Exception,), {})
    session = Session([Timeout("url?secret=oops"), Timeout("again"), Timeout("last")])
    monkeypatch.setattr(fuyao, "_SESSION", session)
    with pytest.raises(fuyao.FuyaoServiceError) as caught:
        fuyao._request("snapshot", "/test", use_cache=False)
    assert "secret" not in str(caught.value)
    assert len(session.calls) == 3


def test_http_auth_is_not_retried(monkeypatch):
    session = Session([Response({}, status=401)])
    monkeypatch.setattr(fuyao, "_SESSION", session)
    with pytest.raises(fuyao.FuyaoAuthenticationError):
        fuyao._request("snapshot", "/test", use_cache=False)
    assert len(session.calls) == 1


def test_historical_mapping_adjustment_and_timezone(monkeypatch):
    monkeypatch.setattr(fuyao, "_SESSION", Session([Response({
        "code": 0, "request_id": "request_hist", "data": {
            "timestamp": stamp("2026-09-14 16:00:00"), "item": [{
                "date_ms": stamp("2026-09-11 00:00:00"), "open_price": 9,
                "high_price": 11, "low_price": 8, "close_price": 10,
                "volume": 1000, "turnover": 10000,
            }]}
    })]))
    frame = fuyao.get_kline("600000", adjust="qfq", use_cache=False)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.index[0] == pd.Timestamp("2026-09-11")
    assert frame.attrs["provenance"]["adjustment"] == "forward"


def test_calendar_maps_closed_days_only_inside_covered_window(monkeypatch):
    monkeypatch.setattr(fuyao, "_SESSION", Session([Response({
        "code": 0, "request_id": "request_cal", "data": {"item": [
            {"date": "2026-09-11"}, {"date": "2026-09-14"}, {"date": "2026-09-15"}
        ]}
    })]))
    assert fuyao.get_trade_calendar_evidence("2026-09-11", "2026-09-14", use_cache=False) == [
        ("2026-09-11", True), ("2026-09-12", False),
        ("2026-09-13", False), ("2026-09-14", True),
    ]


def test_intraday_and_post_close_freshness_are_distinct():
    intraday = fuyao.snapshot_freshness(
        stamp("2026-09-14 10:00:00"), now=datetime.fromisoformat("2026-09-14T10:02:00+08:00")
    )
    closing = fuyao.snapshot_freshness(
        stamp("2026-09-14 15:00:00"), now=datetime.fromisoformat("2026-09-14T20:00:00+08:00")
    )
    assert intraday == {"freshness": "live_current", "stale": False,
                        "market_as_of": "2026-09-14"}
    assert closing == {"freshness": "closing_current", "stale": False,
                       "market_as_of": "2026-09-14"}


def test_capital_flow_is_explicitly_degraded_without_request():
    assert fuyao.capability_status()["capital_flow"]["status"] == "degraded"


@pytest.mark.parametrize("separator", ["=", ":"])
def test_external_secret_file_formats_are_supported_without_exposure(tmp_path, monkeypatch, separator):
    secret = tmp_path / "apikey.env"
    secret.write_text(f"fuyao-aicubes{separator}file-secret-value\n", encoding="utf-8")
    monkeypatch.delenv("FUYAO_AICUBES_API_KEY", raising=False)
    monkeypatch.setenv("FUYAO_AICUBES_API_KEY_FILE", str(secret))
    assert fuyao.api_key() == "file-secret-value"
    assert "file-secret-value" not in repr(fuyao.capability_status())


def test_safe_call_degrades_when_circuit_gate_refuses(monkeypatch):
    @contextmanager
    def refused(*_args, **_kwargs):
        raise RuntimeError("circuit_open")
        yield
    monkeypatch.setattr(fuyao, "source_call", refused)
    monkeypatch.setattr(fuyao, "_SESSION", Session([]))
    assert fuyao._safe_call("snapshot", "/test", use_cache=False) is None
    assert fuyao.capability_status()["capabilities"]["snapshot"]["status"] == "degraded"


def test_formal_daily_gap_repair_prefers_fuyao_qfq(monkeypatch):
    from data import research_sync

    frame = pd.DataFrame(
        {"Open": [9], "High": [11], "Low": [8], "Close": [10], "Volume": [1000]},
        index=pd.DatetimeIndex(["2026-09-11"], name="Date"),
    )
    monkeypatch.setattr(research_sync.fuyao_aicubes, "available", lambda: True)
    monkeypatch.setattr(research_sync.fuyao_aicubes, "get_kline", lambda *_args: frame)
    monkeypatch.setattr(research_sync.baostock, "kline",
                        lambda *_args: pytest.fail("BaoStock should not be called"))
    row = research_sync._fallback_qfq_bar("600000", "2026-09-11")
    assert len(row) == 1
    assert row.attrs["provenance"]["provider"] == "fuyao_aicubes"


def test_calendar_fetch_adds_fuyao_as_independent_recent_validator(monkeypatch):
    from data import research_sync

    today = datetime.now().date().isoformat()
    evidence = [(today, True)]
    monkeypatch.setattr(research_sync.fuyao_aicubes, "available", lambda: True)
    monkeypatch.setattr(research_sync.fuyao_aicubes, "get_trade_calendar_evidence",
                        lambda *_args: evidence)
    monkeypatch.setattr(research_sync.zzshare, "get_trade_calendar_evidence",
                        lambda *_args: evidence)
    monkeypatch.setattr(research_sync.baostock, "trade_calendar_evidence",
                        lambda *_args: evidence)
    actual, failures = research_sync._fetch_calendar_sources(
        today, today, timeout_seconds=1
    )
    assert failures == {}
    assert set(actual) == {"fuyao_aicubes", "zzshare", "baostock"}
