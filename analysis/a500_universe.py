"""Verified, session-local A500 constituents for the market breadth gate."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
CACHE_KEY = "market_breadth:a500:constituents:v1"
STATUS_KEY = "market_breadth:a500:constituents_status:v1"


def _failure(code: str, *, source_date: str | None = None,
             error_type: str | None = None) -> dict:
    return {"available": False, "failure_code": code, "source_date": source_date,
            "error_type": error_type}


def current(*, now: datetime | None = None, cache_get=None) -> dict:
    """Only accept a list fetched and verified during this Shanghai calendar day."""
    if cache_get is None:
        from cache import cache_get
    today = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date().isoformat()
    value = cache_get(CACHE_KEY)
    if (isinstance(value, dict) and value.get("fetched_on") == today
            and value.get("source") == "csindex" and value.get("index_code") == "000510"
            and isinstance(value.get("codes"), list) and len(value["codes"]) == 500
            and len(set(value["codes"])) == 500
            and all(isinstance(code, str) and len(code) == 6 and code.isdigit()
                    for code in value["codes"])):
        return {"available": True, **value}
    status = cache_get(STATUS_KEY)
    if isinstance(status, dict) and status.get("fetched_on") == today:
        return _failure(str(status.get("failure_code") or "a500_constituents_missing"),
                        source_date=status.get("source_date"),
                        error_type=status.get("error_type"))
    return _failure("a500_constituents_missing")


def refresh(*, now: datetime | None = None, calendar_consensus=None,
            fetcher=None, cache_get=None, cache_set=None) -> dict:
    """Fetch the official list; the source date must be the last confirmed session.

    A prior session is current when no trading session has occurred since it.
    The fetch itself must happen today, so an expired holiday cache cannot pass.
    """
    instant = now or datetime.now(SHANGHAI)
    local = instant.astimezone(SHANGHAI) if instant.tzinfo else instant.replace(tzinfo=SHANGHAI)
    today = local.date().isoformat()
    if cache_get is None or cache_set is None:
        from cache import cache_get as default_get, cache_set as default_set
        cache_get = cache_get or default_get
        cache_set = cache_set or default_set
    if calendar_consensus is None:
        from data.research_store import ResearchStore
        calendar_consensus = ResearchStore(ensure_schema=False).calendar_consensus
    try:
        calendar = calendar_consensus(today, inclusive=False)
        expected = str(calendar.get("latest_confirmed_open_date") or "")
        if not calendar.get("ready") or not expected or expected >= today:
            raise ValueError("calendar_not_confirmed")
    except Exception as exc:
        result = _failure("a500_constituents_calendar_unconfirmed",
                          error_type=type(exc).__name__)
        cache_set(STATUS_KEY, {**result, "fetched_on": today}, 86400)
        return result
    try:
        if fetcher is None:
            import akshare as ak
            from akshare_safe import call as ak_call
            fetcher = lambda: ak_call(ak.index_stock_cons_csindex, symbol="000510", timeout=20)
        frame = fetcher()
        if frame is None or "日期" not in frame or "指数代码" not in frame or "成分券代码" not in frame:
            result = _failure("a500_constituents_schema_invalid")
        else:
            dates = {str(value)[:10] for value in frame["日期"].tolist()}
            source_date = next(iter(dates)) if len(dates) == 1 else None
            index_codes = {str(value).zfill(6) for value in frame["指数代码"].tolist()}
            codes = [str(value).zfill(6) for value in frame["成分券代码"].tolist()]
            if source_date != expected or index_codes != {"000510"}:
                result = _failure("a500_constituents_asof_mismatch", source_date=source_date)
            elif (len(codes) != 500 or len(set(codes)) != 500
                  or any(len(code) != 6 or not code.isdigit() for code in codes)):
                result = _failure("a500_constituents_invalid", source_date=source_date)
            else:
                value = {"source": "csindex", "index_code": "000510",
                         "source_date": source_date, "fetched_on": today,
                         "fetched_at": local.isoformat(timespec="seconds"), "codes": codes}
                if cache_set(CACHE_KEY, value, 36 * 3600):
                    return {"available": True, **value}
                result = _failure("a500_constituents_cache_unavailable", source_date=source_date)
    except Exception as exc:
        result = _failure("a500_constituents_source_failed",
                          error_type=type(exc).__name__)
    cache_set(STATUS_KEY, {**result, "fetched_on": today}, 86400)
    return result
