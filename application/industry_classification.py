"""Read-only, point-in-time Shenwan industry and optional manual concepts.

The official StockClassifyUse_stock.xls contains stock codes, effective dates,
six-digit industry codes and update times, but no industry names or themes. An
operator converts it to UTF-8 CSV without editing the source workbook and sets
FOLIANT_SW_CLASSIFICATION_CSV to the resulting external file.
Selected current concept memberships are loaded separately from an external
FOLIANT_MANUAL_CONCEPTS_JSON file and never backfilled before observation.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from functools import lru_cache
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from application.manual_concepts import classify_manual_concepts


HEADERS = ("股票代码", "计入日期", "行业代码", "更新日期")
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MIN_COVERAGE = 0.95
PEER_HORIZONS = (20, 60)
MIN_PEER_HISTORY_ROWS = max(PEER_HORIZONS) + 1
MIN_COMPARABLE_PEERS = 2
MIN_PRUNING_PEERS = 3
MAX_PRIOR_CLOSE_CALENDAR_LAG_DAYS = 10
SOURCE_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
_CODE = re.compile(r"^[0-9]{6}$")

# Reconciled from the 31 Shenwan 2021 L1 constituent workbooks supplied on
# 2026-09-16. Each code/name pair had exact stock+effective-time corroboration
# in StockClassifyUse_stock.xls; the latest 5,274 constituent memberships had
# zero L1 disagreements with that history. L2/L3 names are not in those files.
SW2021_L1_NAMES = {
    "11": "农林牧渔", "22": "基础化工", "23": "钢铁", "24": "有色金属",
    "27": "电子", "28": "汽车", "33": "家用电器", "34": "食品饮料",
    "35": "纺织服饰", "36": "轻工制造", "37": "医药生物",
    "41": "公用事业", "42": "交通运输", "43": "房地产",
    "45": "商贸零售", "46": "社会服务", "48": "银行",
    "49": "非银金融", "51": "综合", "61": "建筑材料",
    "62": "建筑装饰", "63": "电力设备", "64": "机械设备",
    "65": "国防军工", "71": "计算机", "72": "传媒",
    "73": "通信", "74": "煤炭", "75": "石油石化",
    "76": "环保", "77": "美容护理",
}


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
        "l1_name_source": "swsresearch-sw2021-31-l1-constituent-workbooks-2026-09-16",
        "l1_name_source_validation": {
            "history_csv_sha256": "8f904874e07abd943af6ee23bee773f2ea894d8d1166fcf1b4bbcaff013040d9",
            "exact_history_joins": 4111, "latest_memberships_reconciled": 5274,
            "latest_membership_conflicts": 0,
        },
        "csv_sha256": digest, "row_count": len(rows),
        "observed_at": datetime.fromtimestamp(
            mtime_ns / 1e9, tz=ZoneInfo("Asia/Shanghai")
        ).isoformat(),
    }


def classify_symbols(symbols: list[str], *, as_of: str) -> dict[str, Any]:
    """Classify the entire supplied universe; display limits belong to the caller."""
    stocks = sorted(set(symbols))
    base: dict[str, Any] = {
        "status": "missing", "as_of": as_of, "stock_count": len(stocks),
        "coverage": None, "rows": [], "unknown_symbols": stocks,
        "conflict_symbols": [], "industry_coverage_gate": False,
        "l1_name_status": "missing", "industry_groups": [],
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
            "industry_l1_name": SW2021_L1_NAMES.get(code[:2]),
            "industry_l2_code": code[:4], "industry_l3_code": code,
            "effective_date": latest[:10], "effective_at": latest,
            "source_updated_at": winner["updated_at"],
            "industry_name_status": (
                "l1_only" if code[:2] in SW2021_L1_NAMES else "missing"
            ),
        })
    groups: dict[str, list[str]] = {}
    for row in classified:
        groups.setdefault(row["industry_l1_code"], []).append(row["symbol"])
    industry_groups = [
        {"industry_l1_code": code, "industry_l1_name": SW2021_L1_NAMES.get(code),
         "holding_count": len(symbols), "symbols": symbols}
        for code, symbols in groups.items()
    ]
    industry_groups.sort(key=lambda row: (-row["holding_count"], row["industry_l1_code"]))
    coverage = len(classified) / len(stocks) if stocks else None
    gate = coverage is not None and coverage >= MIN_COVERAGE and not conflicts
    base.update({
        "status": "complete" if gate else "degraded",
        "coverage": round(coverage, 6) if coverage is not None else None,
        "rows": classified, "unknown_symbols": unknown,
        "conflict_symbols": conflicts, "industry_coverage_gate": gate,
        "l1_name_status": (
            "complete" if classified and all(row["industry_l1_name"] for row in classified)
            else "partial" if classified else "missing"
        ),
        "industry_groups": industry_groups,
        "source": source,
    })
    return base


def _history_metric(
    symbol: str, frame: Any, *, expected_market_date: str,
    allow_prior_close: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one cache-only qfq series and derive multi-horizon returns."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None, "peer_history_missing"
    if not isinstance(frame.index, pd.DatetimeIndex):
        return None, "peer_history_dates_invalid"
    history = frame.copy()
    if history.index.tz is not None:
        history.index = history.index.tz_convert(ZoneInfo("Asia/Shanghai")).tz_localize(None)
    history = history.sort_index()
    if history.index.hasnans or history.index.normalize().has_duplicates:
        return None, "peer_history_dates_invalid"
    close_column = next(
        (column for column in history.columns if str(column).lower() == "close"), None
    )
    if close_column is None:
        return None, "peer_history_close_missing"
    boundary = pd.Timestamp(expected_market_date)
    closes = pd.to_numeric(
        history.loc[history.index.normalize() <= boundary, close_column], errors="coerce",
    )
    if len(closes) < MIN_PEER_HISTORY_ROWS:
        return None, "peer_history_incomplete"
    values = closes.tail(MIN_PEER_HISTORY_ROWS)
    if (not all(math.isfinite(float(value)) for value in values)
            or (values <= 0).any()):
        return None, "peer_history_values_invalid"
    market_as_of = values.index[-1].date().isoformat()
    lag_days = (date.fromisoformat(expected_market_date)
                - date.fromisoformat(market_as_of)).days
    if lag_days < 0:
        return None, "peer_history_after_expected_date"
    if lag_days and (
        not allow_prior_close or lag_days > MAX_PRIOR_CLOSE_CALENDAR_LAG_DAYS
    ):
        return None, "peer_history_not_current"
    returns = {
        f"return_{horizon}d_pct": round(
            (float(values.iloc[-1]) / float(values.iloc[-horizon - 1]) - 1) * 100, 4,
        )
        for horizon in PEER_HORIZONS
    }
    trace = {
        "symbol": symbol,
        "market_as_of": market_as_of,
        "market_date_status": (
            "current" if lag_days == 0 else "prior_close_reference"
        ),
        "market_date_lag_days": lag_days,
        "history_rows": len(values),
        **returns,
        "input_hash": hashlib.sha256(json.dumps({
            "dates": [item.isoformat() for item in values.index],
            "closes": [round(float(value), 8) for value in values],
        }, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest(),
        "source": str(frame.attrs.get("datahub_source") or "injected_cache"),
        "cache_stale": bool(frame.attrs.get("datahub_stale", False)),
        "cache_age_days": frame.attrs.get("datahub_cache_age_days"),
    }
    cached_at = frame.attrs.get("datahub_cache_written_at")
    if cached_at is not None:
        try:
            trace["cache_written_at"] = datetime.fromtimestamp(
                float(cached_at), ZoneInfo("Asia/Shanghai")
            ).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return trace, None


def _rank_group(
    *, kind: str, group_id: str, group_name: str | None, symbols: list[str],
    metrics: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    comparable = [metrics[symbol] for symbol in sorted(set(symbols)) if symbol in metrics]
    if len(comparable) < MIN_COMPARABLE_PEERS:
        return None
    medians = {
        horizon: float(pd.Series([
            row[f"return_{horizon}d_pct"] for row in comparable
        ]).median())
        for horizon in PEER_HORIZONS
    }
    percentile_by_horizon: dict[int, dict[str, float]] = {}
    for horizon in PEER_HORIZONS:
        series = pd.Series({
            row["symbol"]: row[f"return_{horizon}d_pct"] for row in comparable
        })
        ranks = series.rank(method="average", ascending=False)
        denominator = max(len(series) - 1, 1)
        percentile_by_horizon[horizon] = {
            symbol: round((len(series) - float(rank)) / denominator * 100, 2)
            for symbol, rank in ranks.items()
        }
    rows = []
    for metric in comparable:
        symbol = metric["symbol"]
        horizon_percentiles = {
            f"peer_percentile_{horizon}d": percentile_by_horizon[horizon][symbol]
            for horizon in PEER_HORIZONS
        }
        score = round(sum(horizon_percentiles.values()) / len(PEER_HORIZONS), 2)
        rows.append({
            **metric,
            **horizon_percentiles,
            "relative_strength_score": score,
            **{
                f"excess_vs_group_median_{horizon}d_pct": round(
                    metric[f"return_{horizon}d_pct"] - medians[horizon], 4,
                )
                for horizon in PEER_HORIZONS
            },
        })
    rows.sort(key=lambda row: (-row["relative_strength_score"], row["symbol"]))
    for rank, row in enumerate(rows, start=1):
        row["peer_rank"] = rank
        row["peer_count"] = len(rows)
    return {
        "group_kind": kind,
        "group_id": group_id,
        "group_name": group_name,
        "holding_count": len(set(symbols)),
        "comparable_count": len(rows),
        "market_as_of": min(row["market_as_of"] for row in rows),
        "median_return_20d_pct": round(medians[20], 4),
        "median_return_60d_pct": round(medians[60], 4),
        "rows": rows,
    }


def _peer_comparison(
    *, stocks: list[str], classification: dict[str, Any], themes: dict[str, Any],
    expected_market_date: str, history_loader: Callable[[str], Any] | None,
    stock_names: dict[str, str], allow_prior_close: bool,
) -> dict[str, Any]:
    methodology = {
        "version": "cache-only-multi-horizon-v1",
        "adjustment": "qfq",
        "horizons_trading_days": list(PEER_HORIZONS),
        "relative_strength": "mean_of_within_group_20d_and_60d_return_percentiles",
        "single_day_return_used": False,
        "minimum_comparable_peers": MIN_COMPARABLE_PEERS,
        "minimum_pruning_peers": MIN_PRUNING_PEERS,
        "prior_close_reference_allowed": allow_prior_close,
        "maximum_prior_close_calendar_lag_days": MAX_PRIOR_CLOSE_CALENDAR_LAG_DAYS,
        "pruning_observation_conditions": [
            "same_group_has_at_least_3_comparable_holdings",
            "relative_strength_ranks_in_bottom_third",
            "20d_and_60d_returns_both_below_group_median",
            "confirm_on_at_least_2_consecutive_scheduled_snapshots",
            "combine_with_fundamentals_and_existing_rule_plan_before_human_decision",
        ],
        "execution_price_authority": False,
        "auto_execution": False,
    }
    blockers = []
    if not classification.get("industry_coverage_gate"):
        blockers.append("industry_coverage_gate_failed")
    multi_member_symbols = sorted({
        symbol
        for group in classification.get("industry_groups") or []
        if int(group.get("holding_count") or 0) >= MIN_COMPARABLE_PEERS
        for symbol in group.get("symbols") or []
    })
    if not multi_member_symbols:
        blockers.append("no_multi_member_industry_groups")
    multi_member_theme_symbols = {
        symbol
        for group in themes.get("theme_groups") or []
        if int(group.get("holding_count") or 0) >= MIN_COMPARABLE_PEERS
        for symbol in group.get("symbols") or []
    }
    requested_history_symbols = sorted(
        set(multi_member_symbols) | multi_member_theme_symbols
    )
    if requested_history_symbols and history_loader is None:
        blockers.append("peer_history_loader_unavailable")
    metrics: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    if history_loader is not None:
        for symbol in requested_history_symbols:
            try:
                metric, failure = _history_metric(
                    symbol, history_loader(symbol),
                    expected_market_date=expected_market_date,
                    allow_prior_close=allow_prior_close,
                )
            except Exception:
                metric, failure = None, "peer_history_load_failed"
            if metric is not None:
                if stock_names.get(symbol):
                    metric["name"] = stock_names[symbol]
                metrics[symbol] = metric
            elif failure:
                failures[symbol] = failure
    industry_groups = [
        group for item in classification.get("industry_groups") or []
        if (group := _rank_group(
            kind="industry_l1",
            group_id=str(item.get("industry_l1_code") or ""),
            group_name=item.get("industry_l1_name"),
            symbols=list(item.get("symbols") or []), metrics=metrics,
        )) is not None
    ]
    theme_groups = [
        group for item in themes.get("theme_groups") or []
        if (group := _rank_group(
            kind="theme",
            group_id=str(item.get("thscode") or ""), group_name=item.get("label"),
            symbols=list(item.get("symbols") or []), metrics=metrics,
        )) is not None
    ]
    groups = industry_groups + theme_groups
    if not industry_groups and multi_member_symbols:
        blockers.append("no_industry_group_meets_history_quality_gate")
    comparable_industry_symbols = {
        row["symbol"] for group in industry_groups for row in group["rows"]
    }
    missing_multi_member = sorted(set(multi_member_symbols) - comparable_industry_symbols)
    lagging_industry_symbols = sorted({
        row["symbol"] for group in industry_groups for row in group["rows"]
        if int(row.get("market_date_lag_days") or 0) > 0
    })
    available = bool(industry_groups) and classification.get("industry_coverage_gate") is True
    status = (
        "complete" if available and not missing_multi_member
        and not lagging_industry_symbols else
        "partial" if available else "blocked"
    )
    if missing_multi_member:
        blockers.append("peer_history_coverage_incomplete")
    if lagging_industry_symbols:
        blockers.append("peer_history_prior_close_reference")
    observations = []
    for group in industry_groups:
        count = group["comparable_count"]
        if count < MIN_PRUNING_PEERS:
            continue
        bottom_count = max(1, math.ceil(count / 3))
        for row in group["rows"][-bottom_count:]:
            if (row["excess_vs_group_median_20d_pct"] < 0
                    and row["excess_vs_group_median_60d_pct"] < 0):
                observation = {
                    "symbol": row["symbol"],
                    "group_kind": group["group_kind"],
                    "group_id": group["group_id"],
                    "group_name": group["group_name"],
                    "peer_rank": row["peer_rank"],
                    "peer_count": row["peer_count"],
                    "relative_strength_score": row["relative_strength_score"],
                    "return_20d_pct": row["return_20d_pct"],
                    "return_60d_pct": row["return_60d_pct"],
                    "excess_vs_group_median_20d_pct": row[
                        "excess_vs_group_median_20d_pct"
                    ],
                    "excess_vs_group_median_60d_pct": row[
                        "excess_vs_group_median_60d_pct"
                    ],
                    "market_as_of": row["market_as_of"],
                    "status": "watch_pending_consecutive_confirmation",
                    "required_consecutive_snapshots": 2,
                    "trade_action": None,
                    "execution_price": None,
                }
                if row.get("name"):
                    observation["name"] = row["name"]
                observations.append(observation)
    failure_categories = []
    if status != "complete" and lagging_industry_symbols:
        failure_categories.append("data_lag")
    if (status != "complete" and requested_history_symbols
            and (failures or history_loader is None)):
        failure_categories.append("data_gap")
    if status != "complete" and (
        not classification.get("industry_coverage_gate") or not multi_member_symbols
    ):
        failure_categories.append("quality_gate")
    failure_category = (
        None if not failure_categories else
        failure_categories[0] if len(failure_categories) == 1 else "mixed"
    )
    return {
        "peer_comparison_engine_status": "enabled",
        "peer_comparison_status": status,
        "peer_comparison_available": available,
        "peer_comparison_failure_category": failure_category,
        "peer_comparison_failure_categories": failure_categories,
        "peer_comparison_blockers": list(dict.fromkeys(blockers)),
        "peer_groups": groups[:100],
        "industry_peer_group_count": len(industry_groups),
        "theme_peer_group_count": len(theme_groups),
        "pruning_status": status if available else "blocked",
        "pruning_observations": observations[:100],
        "pruning_observation_count": len(observations),
        "peer_data_quality": {
            "status": status,
            "expected_market_date": expected_market_date,
            "requested_stock_count": len(stocks),
            "requested_history_stock_count": len(requested_history_symbols),
            "history_usable_count": len(metrics),
            "multi_member_industry_stock_count": len(multi_member_symbols),
            "comparable_industry_stock_count": len(comparable_industry_symbols),
            "missing_multi_member_symbols": missing_multi_member[:100],
            "prior_close_reference_symbols": lagging_industry_symbols[:100],
            "market_as_of_dates": sorted({
                row["market_as_of"] for group in industry_groups for row in group["rows"]
            }),
            "failure_by_symbol": {
                symbol: failures[symbol] for symbol in sorted(failures)[:100]
            },
            "cache_only": True,
        },
        "peer_methodology": methodology,
    }


def classify_holdings(
    holdings: list[dict[str, Any]], asset_types: dict[str, str], *, as_of: str,
    expected_market_date: str | None = None,
    history_loader: Callable[[str], Any] | None = None,
    allow_prior_close: bool = False,
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
    stock_names = {
        symbol: str(row.get("name") or "").strip()
        for row in holdings
        if (symbol := str(row.get("symbol") or row.get("code") or "").zfill(6)) in stocks
        and str(row.get("name") or "").strip()
    }
    base: dict[str, Any] = {
        "status": "missing", "as_of": as_of, "stock_count": len(stocks),
        "excluded_fund_count": len(funds), "coverage": None, "rows": [],
        "unknown_symbols": stocks, "conflict_symbols": [],
        "industry_coverage_gate": False, "l1_name_status": "missing",
        "industry_groups": [], "theme_status": "missing",
        "peer_comparison_status": "blocked", "pruning_status": "blocked",
        "auto_execution": False,
    }
    themes = classify_manual_concepts(stocks, as_of=as_of)
    base.update({key: value for key, value in themes.items() if key != "theme_by_symbol"})
    classification = classify_symbols(stocks, as_of=as_of)
    base.update(classification)
    for row in base["rows"]:
        evidence = themes["theme_by_symbol"].get(row["symbol"], [])
        row["theme_labels"] = [item["label"] for item in evidence]
        row["theme_evidence"] = evidence
    base["rows"] = base["rows"][:100]
    base.update(_peer_comparison(
        stocks=stocks, classification=classification, themes=themes,
        expected_market_date=expected_market_date or as_of[:10],
        history_loader=history_loader, stock_names=stock_names,
        allow_prior_close=allow_prior_close,
    ))
    return base
