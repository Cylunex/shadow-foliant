"""Shared fund identity, quality and precision rules; no I/O."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from zoneinfo import ZoneInfo
import json
import math
import re

VERSION = "fund-v2"
TZ = ZoneInfo("Asia/Shanghai")
STATES = {"ready", "partial", "pending_disclosure", "stale", "unsupported", "error"}


def now():
    return datetime.now(TZ).isoformat()


def code6(value):
    if value is None or not str(value).strip():
        raise ValueError("基金代码不能为空")
    value = str(value).strip().zfill(6)
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("基金代码必须为六位数字")
    return value


def number(value, *, positive=False):
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("数值必须有限" + ("且大于零" if positive else ""))
    return result


def decimal(value, places="0.01"):
    number(value)
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def day(value):
    text = str(value)
    if len(text) > 10:
        return instant(text).astimezone(TZ).date().isoformat()
    return date.fromisoformat(text).isoformat()


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed


def fingerprint(value):
    return sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False
        ).encode()
    ).hexdigest()


def product(code, name="", ftype="", **facts):
    text = f"{name or ''} {ftype or ''}"
    category = (
        "money"
        if "货币" in text
        else "bond"
        if "债" in text
        else "qdii"
        if "QDII" in text.upper()
        else "index"
        if any(x in text for x in ("指数", "ETF", "联接"))
        else "allocation"
        if "FOF" in text.upper()
        else "equity"
        if any(k in text for k in ("股票", "混合", "成长", "价值", "精选"))
        else "unknown"
    )
    # Family identity is explicit; names alone are insufficient to merge share classes.
    return {
        "code": code6(code),
        "name": name,
        "ftype": ftype,
        "category": category,
        "asset": {
            "money": "现金",
            "bond": "固收",
            "equity": "权益",
            "index": "未知",
            "qdii": "未知",
            "allocation": "配置",
            "unknown": "未知",
        }[category],
        "currency": None,
        "region": None,
        "family_id": None,
        "benchmark": None,
        "execution": "exchange"
        if ("场内" in text or ("ETF" in text and "联接" not in text))
        else "off_exchange",
        **facts,
    }


def audit_nav(frame, start=None, end=None):
    import pandas as pd

    reasons = list((frame.attrs.get("quality") or {}).get("reasons", []))
    original = frame.attrs.get("quality") or {}
    if frame.empty or not {"date", "unit_nav"}.issubset(frame):
        return {"status": "error", "reasons": ["nav_missing"], "rows": 0}
    dates = pd.to_datetime(frame["date"], errors="coerce")
    values = pd.to_numeric(frame["unit_nav"], errors="coerce")
    if dates.isna().any():
        reasons.append("invalid_date")
    if dates.duplicated().any():
        reasons.append("duplicate_date")
    if (
        (values <= 0) | ~values.map(lambda x: pd.notna(x) and math.isfinite(float(x)))
    ).any():
        reasons.append("invalid_nav")
    if any(str(d.date()) > now()[:10] for d in dates.dropna()):
        reasons.append("future_nav")
    valid = dates.dropna()
    first = valid.min().strftime("%Y-%m-%d") if len(valid) else None
    last = valid.max().strftime("%Y-%m-%d") if len(valid) else None
    if start and (first is None or first > day(start)):
        reasons.append("start_not_covered")
    if end and (last is None or last < day(end)):
        reasons.append("end_not_covered")
    expected = frame.attrs.get("expected_dates")
    if expected is not None and set(map(day, expected)) - set(
        d.strftime("%Y-%m-%d") for d in valid
    ):
        reasons.append("expected_dates_missing")
    if original.get("status") in ("partial", "error"):
        reasons.append("source_incomplete")
    return {
        "status": "partial" if reasons else "ready",
        "reasons": sorted(set(reasons)),
        "rows": len(frame),
        "start": first,
        "end": last,
        "requested_start": start,
        "requested_end": end,
        "source": frame.attrs.get("source"),
        "upstream": frame.attrs.get("upstream"),
        "retrieved_at": frame.attrs.get("retrieved_at"),
        "published_at": frame.attrs.get("published_at"),
        "snapshot_id": fingerprint(
            frame.astype(object).where(frame.notna(), None).to_dict("records")
        ),
    }


def nav_state(info, as_of, *, expected_date=None):
    if not info or not info.get("nav_date") or not info.get("unit_nav"):
        return "partial"
    try:
        number(info["unit_nav"], positive=True)
    except (TypeError, ValueError):
        return "error"
    observed = day(info["nav_date"])
    if observed > day(as_of):
        return "error"
    if observed == day(as_of):
        return "ready"
    # Only a supplied, product-specific expected disclosure date can establish staleness.
    return (
        "stale"
        if expected_date and observed < day(expected_date)
        else "pending_disclosure"
    )


def daily_result(holding, info, as_of, transactions=()):
    """Close-to-close economic P&L with dated flows; missing facts stay unknown."""
    shares = number(holding.get("shares") or 0)
    state = nav_state(info, as_of, expected_date=holding.get("expected_nav_date"))
    nav = info.get("unit_nav") if info else None
    value = shares * float(nav) if nav and state != "error" else None
    prev = (info or {}).get("prev_nav")
    pnl = denominator = None
    if state == "ready" and prev and float(prev) > 0:
        delta = sum(float(t.get("delta_shares") or 0) for t in transactions)
        opening = shares - delta
        cash_paid = sum(float(t.get("cash_paid") or 0) for t in transactions)
        cash_received = sum(float(t.get("cash_received") or 0) for t in transactions)
        if opening >= -1e-8:
            denominator = opening * float(prev)
            dividend_recorded = any(
                t.get("txn_type") == "现金分红" for t in transactions
            )
            dividend = (
                0.0
                if dividend_recorded
                else opening
                * (
                    float((info or {}).get("income_per_10000") or 0) / 10000
                    if (info or {}).get("unit_nav_kind") == "accounting_reference"
                    else float((info or {}).get("cash_dividend") or 0)
                )
            )
            pnl = value - denominator - cash_paid + cash_received + dividend
            if float((info or {}).get("split_ratio") or 1) != 1 and not any(
                t.get("txn_type") == "拆分" for t in transactions
            ):
                pnl = None
                state = "partial"
        if (info or {}).get("actions_status") in ("partial", "error", "unknown"):
            pnl = None
    return {
        "state": state,
        "mv": round(value, 2) if value is not None else None,
        "today_pnl": round(pnl, 2) if pnl is not None else None,
        "previous_value": denominator,
        "nav_date": (info or {}).get("nav_date"),
    }


def nav_records(frame):
    """Net asset dates are business dates, not instants in the host timezone."""
    copy = frame.copy()
    import pandas as pd

    copy["date"] = pd.to_datetime(copy["date"]).dt.strftime("%Y-%m-%d")
    return copy.astype(object).where(copy.notna(), None).to_dict("records")


def engine_hash():
    """Bind frozen experiments to the exact local calculation rules, not a mutable label."""
    from pathlib import Path

    root = Path(__file__).parent
    files = (
        "fund_contracts.py",
        "fund_returns.py",
        "fund_fees.py",
        "fund_calendar.py",
        "fund_dca.py",
        "fund_backtest.py",
    )
    return fingerprint(
        {name: sha256((root / name).read_bytes()).hexdigest() for name in files}
    )
