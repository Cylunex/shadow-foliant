"""Bounded synchronization from external providers into the local research store."""

from __future__ import annotations

from datetime import date, timedelta
import os
from typing import Dict, Iterable, List, Optional

import pandas as pd

from data.research_store import ResearchStore
from data.sources import baostock, eltdx, zzshare


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


def _fallback_bar(symbol: str, trade_date: str) -> pd.DataFrame:
    """Use two independent TDX/BaoStock paths; only an exact-date row is accepted."""
    for provider, call in (
        ("eltdx", lambda: eltdx.get_kline(symbol, "day", 20)),
        ("baostock", lambda: baostock.kline(symbol, "1mo", "1d", "qfq")),
    ):
        try:
            frame = call()
        except Exception:
            frame = pd.DataFrame()
        if frame is None or frame.empty:
            continue
        if "date" in frame.columns:
            raw = frame.copy()
            raw["trade_date"] = pd.to_datetime(raw["date"], errors="coerce").dt.date.astype(str)
            rename = {c: c.lower() for c in ("Open", "High", "Low", "Close", "Volume") if c in raw.columns}
            raw = raw.rename(columns=rename)
        else:
            raw = frame.reset_index().copy()
            date_col = "Date" if "Date" in raw.columns else raw.columns[0]
            raw["trade_date"] = pd.to_datetime(raw[date_col], errors="coerce").dt.date.astype(str)
            raw = raw.rename(columns={c: c.lower() for c in ("Open", "High", "Low", "Close", "Volume") if c in raw.columns})
        row = raw[raw["trade_date"] == trade_date].tail(1)
        if row.empty:
            continue
        row = row.copy()
        row["ts_code"] = symbol
        row.attrs["provenance"] = {
            "provider": provider, "origin": "independent_fallback", "as_of": trade_date,
            "effective_at": trade_date, "retrieved_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
            "adjustment": "qfq" if provider == "baostock" else "raw",
            "unit": "price/currency/shares", "schema_version": "1", "quality_status": "fallback",
        }
        return row
    return pd.DataFrame()


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

    def sync_day(self, trade_date: str, *, fundamentals: bool = True,
                 fallback: bool = True) -> dict:
        trade_date = pd.Timestamp(trade_date).date().isoformat()
        run_id = self.store.start_sync("zzshare", "daily_market", trade_date)
        result: Dict[str, object] = {"trade_date": trade_date, "providers": {}}
        try:
            frame = _normalize_market_bars(zzshare.get_market_daily(trade_date), trade_date)
            primary_rows = self.store.upsert_daily_bars(frame, adjustment="qfq")
            result["providers"]["zzshare"] = primary_rows

            universe = self.store.load_universe(trade_date)
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
                    row = _fallback_bar(symbol, trade_date)
                    if not row.empty:
                        adjustment = row.attrs.get("provenance", {}).get("adjustment", "raw")
                        fallback_rows += self.store.upsert_daily_bars(row, adjustment=adjustment)
            result["providers"]["independent_fallback"] = fallback_rows

            valuation = zzshare.get_valuation(trade_date)
            result["valuation_rows"] = self.store.upsert_valuations(valuation, as_of=trade_date)
            result["finance_rows"] = {}
            if fundamentals:
                for table in ("indicator", "income", "balance", "cash_flow"):
                    pit = zzshare.get_finance_pit(table, trade_date)
                    result["finance_rows"][table] = self.store.upsert_financial_pit(
                        table, pit, as_of=trade_date
                    )

            coverage = primary_rows / expected if expected else 0.0
            quality = "ok" if expected and coverage >= minimum_ratio else "incomplete"
            result["expected"] = expected
            result["coverage"] = round(coverage, 6)
            result["quality_status"] = quality
            total_rows = primary_rows + fallback_rows + int(result["valuation_rows"]) + sum(
                int(value) for value in result["finance_rows"].values()
            )
            self.store.finish_sync(run_id, status="success" if primary_rows else "error",
                                   row_count=total_rows, quality_status=quality,
                                   detail={"coverage": result["coverage"], "fallback_rows": fallback_rows})
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
        calendar = zzshare.get_trade_days(start.isoformat(), end.isoformat())
        calendar = [day for day in calendar if day <= end.isoformat()][-int(trading_days):]
        if len(calendar) < min(int(trading_days), 320):
            raise RuntimeError("insufficient trading calendar for research bootstrap")
        completed = 0
        incomplete = 0
        for trade_day in calendar:
            result = self.sync_day(trade_day, fundamentals=False, fallback=False)
            completed += int(result.get("providers", {}).get("zzshare", 0) > 0)
            incomplete += int(result.get("quality_status") != "ok")
        latest = self.sync_day(calendar[-1], fundamentals=True, fallback=True)
        return {
            "requested_days": len(calendar), "completed_days": completed,
            "incomplete_days": incomplete, "latest": latest,
        }
