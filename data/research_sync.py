"""Bounded synchronization from external providers into the local research store."""

from __future__ import annotations

import concurrent.futures
from datetime import date, timedelta
import os
import time
from typing import Dict, Iterable, List, Optional

import pandas as pd

from data.research_store import ResearchStore
from data.sources import baostock, zzshare


_CALENDAR_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="research-calendar"
)


def _calendar_chunks(start_date: str, end_date: str, *, days: int = 92):
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=max(7, int(days)) - 1))
        yield cursor.isoformat(), chunk_end.isoformat()
        cursor = chunk_end + timedelta(days=1)


def _calendar_chunk_quality(evidence, start_date: str, end_date: str) -> tuple[str, dict]:
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    normalized_rows = set()
    invalid_rows = 0
    for day, state in evidence:
        try:
            parsed = pd.Timestamp(day)
            if pd.isna(parsed):
                raise ValueError("missing calendar date")
            normalized_rows.add((parsed.date().isoformat(), bool(state)))
        except (TypeError, ValueError, OverflowError):
            invalid_rows += 1
    normalized = sorted(normalized_rows)
    opens = [pd.Timestamp(day).date() for day, state in normalized if state]
    weekdays = len(pd.bdate_range(start, end))
    reasons = []
    if invalid_rows:
        reasons.append("invalid_calendar_rows")
    if not normalized or not opens:
        reasons.append("empty_calendar_chunk")
    if any(pd.Timestamp(day).date() < start or pd.Timestamp(day).date() > end
           for day, _ in normalized):
        reasons.append("calendar_row_outside_requested_range")
    if weekdays >= 10 and len(opens) < max(3, int(weekdays * 0.45)):
        reasons.append("implausible_open_day_density")
    if opens:
        if (opens[0] - start).days > 10:
            reasons.append("leading_calendar_gap")
        if (end - opens[-1]).days > 10:
            reasons.append("trailing_calendar_gap")
    return ("ok" if not reasons else "incomplete", {
        "reasons": reasons, "row_count": len(normalized), "open_count": len(opens),
        "weekday_count": weekdays, "invalid_row_count": invalid_rows,
    })


def _calendar_source_timeout() -> float:
    try:
        configured = float(os.getenv("RESEARCH_CALENDAR_SOURCE_TIMEOUT_SECONDS", "30"))
    except (TypeError, ValueError):
        configured = 30.0
    return min(120.0, max(5.0, configured))


def _fetch_calendar_sources(start_date: str, end_date: str, *,
                            timeout_seconds: Optional[float] = None) -> tuple[dict, dict]:
    """Fetch independent calendars concurrently under one wall-clock deadline."""
    timeout = (_calendar_source_timeout() if timeout_seconds is None
               else max(0.01, float(timeout_seconds)))
    calls = {
        "zzshare": lambda: zzshare.get_trade_calendar_evidence(start_date, end_date),
        "baostock": lambda: baostock.trade_calendar_evidence(start_date, end_date),
    }
    futures = {
        provider: _CALENDAR_EXECUTOR.submit(call) for provider, call in calls.items()
    }
    deadline = time.monotonic() + timeout
    evidence = {}
    failures = {}
    for provider, future in futures.items():
        try:
            evidence[provider] = future.result(
                timeout=max(0.001, deadline - time.monotonic())
            ) or []
        except concurrent.futures.TimeoutError:
            future.cancel()
            evidence[provider] = []
            failures[provider] = "source_timeout"
        except Exception as exc:
            evidence[provider] = []
            failures[provider] = f"source_error:{type(exc).__name__}"
    return evidence, failures


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "st", "停牌"}
    return bool(value)


def _normalize_market_bars(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    rename = {
        "date": "trade_date", "vol": "volume", "amount": "turnover",
        "pct_chg": "quote_rate",
    }
    out = out.rename(columns={key: value for key, value in rename.items() if key in out.columns})
    if "trade_date" not in out.columns:
        out["trade_date"] = trade_date
    if "ts_code" not in out.columns:
        code_col = next((c for c in ("code", "symbol") if c in out.columns), None)
        if code_col:
            out["ts_code"] = out[code_col]
    return out


def _fallback_qfq_bar(symbol: str, trade_date: str) -> pd.DataFrame:
    """BaoStock alone repairs qfq rows; raw TDX remains a validation/quote source."""
    try:
        frame = baostock.kline(symbol, "1mo", "1d", "qfq")
    except Exception:
        frame = pd.DataFrame()
    if frame is None or frame.empty:
        return pd.DataFrame()
    if "date" in frame.columns:
        raw = frame.copy()
        raw["trade_date"] = pd.to_datetime(raw["date"], errors="coerce").dt.date.astype(str)
        raw = raw.rename(columns={
            c: c.lower() for c in ("Open", "High", "Low", "Close", "Volume") if c in raw.columns
        })
    else:
        raw = frame.reset_index().copy()
        date_col = "Date" if "Date" in raw.columns else raw.columns[0]
        raw["trade_date"] = pd.to_datetime(raw[date_col], errors="coerce").dt.date.astype(str)
        raw = raw.rename(columns={
            c: c.lower() for c in ("Open", "High", "Low", "Close", "Volume") if c in raw.columns
        })
    row = raw[raw["trade_date"] == trade_date].tail(1)
    if row.empty:
        return pd.DataFrame()
    row = row.copy()
    row["ts_code"] = symbol
    row.attrs["provenance"] = {
        "provider": "baostock", "origin": "independent_qfq_fallback", "as_of": trade_date,
        "effective_at": trade_date, "retrieved_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "adjustment": "qfq", "unit": "price/currency/shares", "schema_version": "2",
        "quality_status": "fallback",
    }
    return row


class ResearchSynchronizer:
    def __init__(self, store: Optional[ResearchStore] = None):
        self.store = store or ResearchStore()

    def sync_master(self) -> dict:
        as_of = date.today().isoformat()
        run_id = self.store.start_sync("zzshare", "security_master", as_of)
        try:
            frame = zzshare.get_security_master()
            published = self.store.publish_security_master(
                frame, minimum_rows=int(os.getenv("RESEARCH_MIN_UNIVERSE_ROWS", "3000"))
            )
            rows = int(published["rows"])
            quality = str(published["quality_status"])
            self.store.finish_sync(
                run_id, status="success" if published["published"] else "error",
                row_count=rows, quality_status=quality,
                detail={
                    "snapshot_id": published["snapshot_id"],
                    "published": published["published"],
                    "reasons": published["reasons"],
                    "exchange_counts": published["exchange_counts"],
                },
            )
            return published
        except Exception as exc:
            self.store.finish_sync(run_id, status="error", row_count=0, quality_status="failed",
                                   detail={"error_type": type(exc).__name__})
            raise

    def sync_calendar(self, start_date: str, end_date: str) -> dict:
        """Build explicit open/closed evidence and require zzshare/BaoStock consensus."""
        start = pd.Timestamp(start_date).date().isoformat()
        end = pd.Timestamp(end_date).date().isoformat()
        run_id = self.store.start_sync("consensus", "trade_calendar", end)
        try:
            provider_evidence = {"zzshare": [], "baostock": []}
            incomplete_chunks = []
            for chunk_start, chunk_end in _calendar_chunks(start, end):
                chunks, failures = _fetch_calendar_sources(chunk_start, chunk_end)
                for provider, evidence in chunks.items():
                    quality, chunk_detail = _calendar_chunk_quality(
                        evidence, chunk_start, chunk_end
                    )
                    if provider in failures:
                        quality = "incomplete"
                        chunk_detail["reasons"] = list(dict.fromkeys(
                            [*chunk_detail.get("reasons", []), failures[provider]]
                        ))
                    self.store.record_calendar_fetch(
                        provider, chunk_start, chunk_end, evidence,
                        quality_status=quality, detail=chunk_detail,
                    )
                    if quality == "ok":
                        self.store.replace_calendar_evidence(
                            evidence, provider=provider,
                            start_date=chunk_start, end_date=chunk_end,
                        )
                        provider_evidence[provider].extend(evidence)
                    else:
                        incomplete_chunks.append({
                            "provider": provider, "start": chunk_start, "end": chunk_end,
                            **chunk_detail,
                        })
            primary = {day for day, state in provider_evidence["zzshare"] if state}
            validator = {day for day, state in provider_evidence["baostock"] if state}
            if not primary or not validator:
                raise RuntimeError("independent trade calendar source unavailable")
            confirmed = sorted(primary & validator)
            disagreements = sorted(primary ^ validator)
            self.store.upsert_trade_days(confirmed, provider="zzshare+baostock")
            quality = "ok" if not disagreements and not incomplete_chunks else "incomplete"
            detail = {
                "provider_count": 2,
                "confirmed_open_days": len(confirmed),
                "disagreement_count": len(disagreements),
                "coverage_through_date": end,
                "incomplete_chunk_count": len(incomplete_chunks),
            }
            self.store.finish_sync(
                run_id, status="success", row_count=sum(
                    len(value) for value in provider_evidence.values()
                ),
                quality_status=quality, detail=detail,
            )
            return {**detail, "quality_status": quality, "trade_days": confirmed}
        except Exception as exc:
            self.store.finish_sync(
                run_id, status="error", row_count=0, quality_status="failed",
                detail={"error_type": type(exc).__name__},
            )
            raise

    def sync_day(self, trade_date: str, *, fundamentals: bool = True,
                 fallback: bool = True, refresh_calendar: bool = True) -> dict:
        trade_date = pd.Timestamp(trade_date).date().isoformat()
        if refresh_calendar:
            day = pd.Timestamp(trade_date).date()
            self.sync_calendar(
                (day - timedelta(days=14)).isoformat(), (day + timedelta(days=1)).isoformat()
            )
        run_id = self.store.start_sync("zzshare", "daily_market", trade_date)
        result: Dict[str, object] = {"trade_date": trade_date, "providers": {}}
        try:
            frame = _normalize_market_bars(zzshare.get_market_daily(trade_date), trade_date)
            self.store.upsert_daily_bars(frame, adjustment="qfq")
            primary_rows = self.store.daily_bar_symbol_count(
                trade_date, adjustment="qfq", provider="zzshare"
            )
            result["providers"]["zzshare"] = primary_rows

            # Coverage is an ingestion-health metric, so a weekend/manual sync
            # compares the requested market day with the newest master snapshot.
            # Selection itself still uses load_universe(selection_date) and
            # therefore remains fail-closed against future master data.
            universe = self.store.load_latest_universe()
            expected = len(universe)
            minimum_ratio = float(os.getenv("RESEARCH_DAILY_MIN_COVERAGE", "0.90"))
            missing: List[str] = []
            if expected and primary_rows < int(expected * minimum_ratio):
                got = {
                    "".join(ch for ch in str(value) if ch.isdigit())[-6:]
                    for value in frame.get("ts_code", pd.Series(dtype=str)).tolist()
                }
                missing = [symbol for symbol in universe["symbol"].tolist() if symbol not in got]
            fallback_rows = 0
            if fallback and missing:
                cap = max(0, int(os.getenv("RESEARCH_FALLBACK_SYMBOLS_PER_RUN", "100")))
                for symbol in missing[:cap]:
                    row = _fallback_qfq_bar(symbol, trade_date)
                    if not row.empty:
                        fallback_rows += self.store.upsert_daily_bars(row, adjustment="qfq")
            result["providers"]["baostock_qfq_fallback"] = fallback_rows

            valuation = zzshare.get_valuation(trade_date)
            result["valuation_rows"] = self.store.upsert_valuations(valuation, as_of=trade_date)
            result["finance_rows"] = {}
            if fundamentals:
                for table in ("indicator", "income", "balance", "cash_flow"):
                    pit = zzshare.get_finance_pit(table, trade_date)
                    result["finance_rows"][table] = self.store.upsert_financial_pit(
                        table, pit, as_of=trade_date
                    )

            usable_rows = self.store.daily_bar_symbol_count(trade_date, adjustment="qfq")
            primary_coverage = primary_rows / expected if expected else 0.0
            usable_coverage = usable_rows / expected if expected else 0.0
            valuation_coverage = int(result["valuation_rows"]) / expected if expected else 0.0
            minimum_valuation = max(
                0.70, float(os.getenv("RESEARCH_DAILY_MIN_VALUATION_COVERAGE", "0.70"))
            )
            primary_quality = str(frame.attrs.get("provenance", {}).get("quality_status") or "unknown")
            valuation_quality = str(valuation.attrs.get("provenance", {}).get("quality_status") or "unknown")
            quality = "ok" if (
                expected and usable_coverage >= minimum_ratio
                and valuation_coverage >= minimum_valuation
                and primary_quality not in {"failed", "unknown_unit", "possibly_truncated"}
                and valuation_quality == "ok"
            ) else "incomplete"
            result["expected"] = expected
            result["primary_coverage"] = round(primary_coverage, 6)
            result["usable_qfq_coverage"] = round(usable_coverage, 6)
            result["valuation_coverage"] = round(valuation_coverage, 6)
            result["market_quality_status"] = primary_quality
            result["valuation_quality_status"] = valuation_quality
            result["fallback_repaired_count"] = fallback_rows
            result["coverage"] = result["usable_qfq_coverage"]
            result["quality_status"] = quality
            total_rows = primary_rows + fallback_rows + int(result["valuation_rows"]) + sum(
                int(value) for value in result["finance_rows"].values()
            )
            self.store.finish_sync(run_id, status="success" if primary_rows else "error",
                                   row_count=total_rows, quality_status=quality,
                                   detail={"primary_coverage": result["primary_coverage"],
                                           "usable_qfq_coverage": result["usable_qfq_coverage"],
                                           "valuation_coverage": result["valuation_coverage"],
                                           "market_quality_status": primary_quality,
                                           "valuation_quality_status": valuation_quality,
                                           "fallback_repaired_count": fallback_rows})
            return result
        except Exception as exc:
            self.store.finish_sync(run_id, status="error", row_count=0, quality_status="failed",
                                   detail={"error_type": type(exc).__name__})
            raise

    def bootstrap(self, *, end_date: Optional[str] = None, trading_days: int = 500) -> dict:
        """Build 400-500 trading days without per-symbol zzshare requests."""
        end = pd.Timestamp(end_date or date.today()).date()
        start = end - timedelta(days=max(int(trading_days) * 2, 800))
        self.sync_master()
        calendar_result = self.sync_calendar(start.isoformat(), end.isoformat())
        calendar = [
            day for day in calendar_result["trade_days"] if day <= end.isoformat()
        ][-int(trading_days):]
        if len(calendar) < min(int(trading_days), 320):
            raise RuntimeError("insufficient trading calendar for research bootstrap")
        completed = 0
        incomplete = 0
        skipped = 0
        for trade_day in calendar:
            if self.store.completed_sync("daily_market", trade_day):
                skipped += 1
                continue
            result = self.sync_day(
                trade_day, fundamentals=False, fallback=False, refresh_calendar=False
            )
            completed += int(result.get("providers", {}).get("zzshare", 0) > 0)
            incomplete += int(result.get("quality_status") != "ok")
        latest = self.sync_day(
            calendar[-1], fundamentals=True, fallback=True, refresh_calendar=False
        )
        return {
            "requested_days": len(calendar), "completed_days": completed,
            "skipped_complete_days": skipped,
            "incomplete_days": incomplete, "latest": latest,
        }
