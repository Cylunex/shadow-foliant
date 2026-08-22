"""Bounded synchronization from external providers into the local research store."""

from __future__ import annotations

from datetime import date, timedelta
import os
from typing import Dict, Iterable, List, Optional

import pandas as pd

from data.research_store import ResearchStore
from data.sources import baostock, zzshare


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
            rows = self.store.upsert_securities(frame)
            quality = "ok" if rows >= int(os.getenv("RESEARCH_MIN_UNIVERSE_ROWS", "3000")) else "incomplete"
            self.store.finish_sync(run_id, status="success" if rows else "error",
                                   row_count=rows, quality_status=quality)
            return {"rows": rows, "quality_status": quality}
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
            primary = set(zzshare.get_trade_days(start, end))
            validator = set(baostock.trade_days(start, end))
            if not primary or not validator:
                raise RuntimeError("independent trade calendar source unavailable")
            calendar_days = [
                day.date().isoformat() for day in pd.date_range(start=start, end=end, freq="D")
            ]
            self.store.upsert_calendar_evidence(
                ((day, day in primary) for day in calendar_days), provider="zzshare"
            )
            self.store.upsert_calendar_evidence(
                ((day, day in validator) for day in calendar_days), provider="baostock"
            )
            confirmed = sorted(primary & validator)
            disagreements = sorted(primary ^ validator)
            self.store.upsert_trade_days(confirmed, provider="zzshare+baostock")
            quality = "ok" if not disagreements else "incomplete"
            detail = {
                "provider_count": 2,
                "confirmed_open_days": len(confirmed),
                "disagreement_count": len(disagreements),
                "coverage_through_date": end,
            }
            self.store.finish_sync(
                run_id, status="success", row_count=len(calendar_days) * 2,
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
            quality = "ok" if expected and usable_coverage >= minimum_ratio else "incomplete"
            result["expected"] = expected
            result["primary_coverage"] = round(primary_coverage, 6)
            result["usable_qfq_coverage"] = round(usable_coverage, 6)
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
        for trade_day in calendar:
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
            "incomplete_days": incomplete, "latest": latest,
        }
