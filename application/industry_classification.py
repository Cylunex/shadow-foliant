"""Read-only, point-in-time Shenwan stock industry evidence.

The official StockClassifyUse_stock.xls contains stock codes, effective dates,
six-digit industry codes and update times, but no industry names or themes. An
operator converts it to UTF-8 CSV without editing the source workbook and sets
FOLIANT_SW_CLASSIFICATION_CSV to the resulting external file.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from functools import lru_cache
import hashlib
import io
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo


HEADERS = ("股票代码", "计入日期", "行业代码", "更新日期")
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MIN_COVERAGE = 0.95
SOURCE_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
_CODE = re.compile(r"^[0-9]{6}$")


def _source() -> tuple[tuple[dict[str, str], ...], dict[str, Any]]:
    configured = os.getenv("FOLIANT_SW_CLASSIFICATION_CSV", "").strip()
    if not configured:
        raise ValueError("sw_classification_source_unconfigured")
    path = Path(configured)
    if path.is_symlink() or not path.is_file():
        raise ValueError("sw_classification_source_invalid")
    stat = path.stat()
    if not 0 < stat.st_size <= MAX_SOURCE_BYTES:
        raise ValueError("sw_classification_source_size_invalid")
    return _load(str(path), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=2)
def _load(path: str, size: int, mtime_ns: int) -> tuple[tuple[dict[str, str], ...], dict[str, Any]]:
    raw = Path(path).read_bytes()
    if len(raw) != size:
        raise ValueError("sw_classification_source_changed")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        with io.StringIO(raw.decode("utf-8-sig"), newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != HEADERS:
                raise ValueError("sw_classification_headers_invalid")
            rows = []
            for row in reader:
                symbol = str(row.get("股票代码") or "")
                code = str(row.get("行业代码") or "")
                effective = str(row.get("计入日期") or "")
                updated = str(row.get("更新日期") or "")
                if not _CODE.fullmatch(symbol) or not _CODE.fullmatch(code):
                    raise ValueError("sw_classification_code_invalid")
                effective_time = (datetime.fromisoformat(effective) if len(effective) > 10 else
                                  datetime.combine(date.fromisoformat(effective), datetime.min.time()))
                datetime.fromisoformat(updated)
                rows.append({
                    "symbol": symbol, "industry_code": code,
                    "effective_at": effective_time.isoformat(sep=" "),
                    "updated_at": updated,
                })
    except (UnicodeError, csv.Error, TypeError) as exc:
        raise ValueError("sw_classification_source_unreadable") from exc
    if not rows:
        raise ValueError("sw_classification_source_empty")
    return tuple(rows), {
        "provider": "swsresearch", "source_format": "StockClassifyUse_stock.xls/UTF-8-CSV",
        "source_url": SOURCE_URL,
        "csv_sha256": digest, "row_count": len(rows),
        "observed_at": datetime.fromtimestamp(
            mtime_ns / 1e9, tz=ZoneInfo("Asia/Shanghai")
        ).isoformat(),
    }


def classify_holdings(
    holdings: list[dict[str, Any]], asset_types: dict[str, str], *, as_of: str,
) -> dict[str, Any]:
    """Return bounded account evidence; never infer labels from stock names."""
    stocks = sorted({
        str(row.get("symbol") or row.get("code") or "").zfill(6)
        for row in holdings
        if asset_types.get(str(row.get("symbol") or row.get("code") or "").zfill(6)) == "stock"
    })
    funds = sorted({
        str(row.get("symbol") or row.get("code") or "").zfill(6)
        for row in holdings
        if asset_types.get(str(row.get("symbol") or row.get("code") or "").zfill(6)) == "fund_or_etf_or_lof"
    })
    base: dict[str, Any] = {
        "status": "missing", "as_of": as_of, "stock_count": len(stocks),
        "excluded_fund_count": len(funds), "coverage": None, "rows": [],
        "unknown_symbols": stocks, "conflict_symbols": [],
        "industry_coverage_gate": False, "theme_status": "missing",
        "peer_comparison_status": "blocked", "pruning_status": "blocked",
        "auto_execution": False,
    }
    try:
        decision = (datetime.fromisoformat(as_of) if len(as_of) > 10 else
                    datetime.combine(date.fromisoformat(as_of), datetime.max.time()))
        if decision.tzinfo is not None:
            decision = decision.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        history, source = _source()
    except (OSError, ValueError) as exc:
        base["reason"] = str(exc)
        return base
    # A newly downloaded current snapshot cannot be used as historical PIT
    # evidence before the source was actually available to this installation.
    source_observed = datetime.fromisoformat(source["observed_at"]).replace(tzinfo=None)
    if decision < source_observed:
        base["reason"] = "sw_classification_source_observed_after_decision"
        base["source"] = source
        return base
    relevant: dict[str, list[dict[str, str]]] = {symbol: [] for symbol in stocks}
    for row in history:
        if (row["symbol"] in relevant
                and datetime.fromisoformat(row["effective_at"]) <= decision
                and datetime.fromisoformat(row["updated_at"]) <= decision):
            relevant[row["symbol"]].append(row)
    classified = []
    conflicts = []
    unknown = []
    for symbol in stocks:
        candidates = relevant[symbol]
        if not candidates:
            unknown.append(symbol)
            continue
        latest = max(row["effective_at"] for row in candidates)
        latest_rows = [row for row in candidates if row["effective_at"] == latest]
        codes = {row["industry_code"] for row in latest_rows}
        if len(codes) != 1:
            conflicts.append(symbol)
            continue
        winner = max(latest_rows, key=lambda row: row["updated_at"])
        code = winner["industry_code"]
        classified.append({
            "symbol": symbol, "industry_l1_code": code[:2],
            "industry_l2_code": code[:4], "industry_l3_code": code,
            "effective_date": latest[:10], "effective_at": latest,
            "source_updated_at": winner["updated_at"],
            "industry_name_status": "missing", "theme_labels": [],
        })
    coverage = len(classified) / len(stocks) if stocks else None
    gate = coverage is not None and coverage >= MIN_COVERAGE and not conflicts
    base.update({
        "status": "complete" if gate else "degraded",
        "coverage": round(coverage, 6) if coverage is not None else None,
        "rows": classified[:100], "unknown_symbols": unknown,
        "conflict_symbols": conflicts, "industry_coverage_gate": gate,
        "source": source,
    })
    return base
