"""Local point-in-time research warehouse used by the primary stock selector.

Only normalized market/research data is stored here.  Credentials, sessions,
portfolio positions and trades are intentionally outside this schema.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence
import uuid

import pandas as pd

import _bootstrap
from db_compat import connect as db_connect


DEFAULT_PATH = _bootstrap.db_path("research_market.db")
SCHEMA_VERSION = "2"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str,
                      allow_nan=False)


def _plain(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _symbol(value: object) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())[-6:]
    return digits if len(digits) == 6 else ""


def _iso_date(value: object) -> str:
    try:
        return pd.Timestamp(value).date().isoformat()
    except Exception:
        return ""


class ResearchStore:
    def __init__(self, path: Optional[str] = None, *, connect_fn: Optional[Callable] = None,
                 is_postgres: Optional[bool] = None):
        self.path = str(path or DEFAULT_PATH)
        self._connect_fn = connect_fn or db_connect
        # Explicit test adapters may use an in-memory database. Production never
        # guesses the dialect from a global feature flag: the default connector is PG.
        self._is_postgres = (connect_fn is None) if is_postgres is None else is_postgres
        self.ensure_schema()

    def connect(self):
        return self._connect_fn(self.path)

    def ensure_schema(self) -> None:
        conn = self.connect()
        cur = conn.cursor()
        id_type = "TEXT PRIMARY KEY"
        statements = [
            f"""CREATE TABLE IF NOT EXISTS research_sync_runs (
                run_id {id_type}, provider TEXT NOT NULL, capability TEXT NOT NULL,
                as_of TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
                quality_status TEXT NOT NULL, detail TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS research_trade_calendar (
                trade_date TEXT PRIMARY KEY, provider TEXT NOT NULL,
                retrieved_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS research_security_snapshots (
                snapshot_date TEXT NOT NULL, symbol TEXT NOT NULL, ts_code TEXT NOT NULL,
                name TEXT, exchange TEXT, market TEXT, industry TEXT, list_status TEXT,
                list_date TEXT, delist_date TEXT, is_hs TEXT,
                provider TEXT NOT NULL, origin TEXT NOT NULL, effective_at TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, schema_version TEXT NOT NULL,
                quality_status TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(snapshot_date,symbol)
            )""",
            """CREATE TABLE IF NOT EXISTS research_securities (
                symbol TEXT PRIMARY KEY, ts_code TEXT NOT NULL, name TEXT,
                exchange TEXT, market TEXT, industry TEXT, list_status TEXT,
                list_date TEXT, delist_date TEXT, is_hs TEXT,
                as_of TEXT NOT NULL, provider TEXT NOT NULL, origin TEXT NOT NULL,
                effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                schema_version TEXT NOT NULL, quality_status TEXT NOT NULL,
                payload TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS research_daily_bars (
                symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                adjustment TEXT NOT NULL, open REAL, high REAL, low REAL,
                close REAL, volume REAL, amount REAL, turnover_rate REAL,
                is_paused INTEGER, is_st INTEGER,
                provider TEXT NOT NULL, origin TEXT NOT NULL,
                effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                unit TEXT NOT NULL, schema_version TEXT NOT NULL,
                quality_status TEXT NOT NULL,
                PRIMARY KEY(symbol, trade_date, adjustment)
            )""",
            """CREATE TABLE IF NOT EXISTS research_valuations (
                symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                market_cap REAL, circulating_market_cap REAL,
                turnover_ratio REAL, pe_ttm REAL, pe_lyr REAL, pb REAL, ps REAL, pcf REAL,
                provider TEXT NOT NULL, origin TEXT NOT NULL,
                effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                schema_version TEXT NOT NULL, quality_status TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(symbol, trade_date)
            )""",
            """CREATE TABLE IF NOT EXISTS research_financial_pit (
                table_name TEXT NOT NULL, symbol TEXT NOT NULL, as_of TEXT NOT NULL,
                stat_date TEXT, pub_date TEXT,
                provider TEXT NOT NULL, origin TEXT NOT NULL,
                effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                schema_version TEXT NOT NULL, quality_status TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(table_name, symbol, as_of)
            )""",
            """CREATE TABLE IF NOT EXISTS research_financial_facts (
                table_name TEXT NOT NULL, symbol TEXT NOT NULL, stat_date TEXT NOT NULL,
                pub_date TEXT NOT NULL, revision_no TEXT NOT NULL, provider TEXT NOT NULL,
                first_seen_as_of TEXT NOT NULL, origin TEXT NOT NULL,
                effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                schema_version TEXT NOT NULL, quality_status TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(table_name,symbol,stat_date,pub_date,revision_no,provider)
            )""",
            """CREATE TABLE IF NOT EXISTS research_events (
                event_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, event_type TEXT NOT NULL,
                event_date TEXT NOT NULL, effective_at TEXT NOT NULL,
                direction REAL NOT NULL, confidence REAL NOT NULL,
                source TEXT NOT NULL, official INTEGER NOT NULL DEFAULT 0,
                title TEXT, payload TEXT NOT NULL, retrieved_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS research_event_records (
                event_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, event_type TEXT NOT NULL,
                event_date TEXT NOT NULL, effective_at TEXT NOT NULL,
                direction REAL NOT NULL, confidence REAL NOT NULL, materiality REAL NOT NULL,
                surprise REAL NOT NULL, novelty REAL NOT NULL,
                source_family TEXT NOT NULL, source_origin TEXT NOT NULL,
                document_id TEXT NOT NULL, event_cluster_id TEXT NOT NULL,
                confirmation_status TEXT NOT NULL, entity_impact TEXT NOT NULL,
                official INTEGER NOT NULL DEFAULT 0, title TEXT,
                original_values TEXT NOT NULL, normalized_values TEXT NOT NULL,
                payload TEXT NOT NULL, retrieved_at TEXT NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS selection_runs (
                run_id {id_type}, selection_date TEXT NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL, primary_source TEXT NOT NULL,
                universe_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL,
                final_count INTEGER NOT NULL, coverage REAL NOT NULL,
                reference_source TEXT, comparison TEXT NOT NULL, metadata TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS selection_candidates (
                run_id TEXT NOT NULL, symbol TEXT NOT NULL, candidate_kind TEXT NOT NULL,
                rank_pos INTEGER, total_score REAL, fundamental_score REAL,
                technical_60_score REAL, industry_score REAL, quality_score REAL,
                correction_120 REAL, correction_250 REAL, event_correction REAL,
                data_coverage REAL, state TEXT, industry TEXT,
                reasons TEXT NOT NULL, source_labels TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(run_id, symbol, candidate_kind)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_research_bars_date ON research_daily_bars(trade_date)",
            "CREATE INDEX IF NOT EXISTS idx_research_security_snapshot ON research_security_snapshots(snapshot_date,symbol)",
            "CREATE INDEX IF NOT EXISTS idx_research_financial_visible ON research_financial_facts(table_name,first_seen_as_of,pub_date,symbol)",
            "CREATE INDEX IF NOT EXISTS idx_research_finance_asof ON research_financial_pit(as_of, table_name)",
            "CREATE INDEX IF NOT EXISTS idx_research_events_date ON research_events(event_date, symbol)",
            "CREATE INDEX IF NOT EXISTS idx_research_event_records_date ON research_event_records(event_date,symbol,confirmation_status)",
            "CREATE INDEX IF NOT EXISTS idx_selection_runs_date ON selection_runs(selection_date, created_at)",
        ]
        try:
            for sql in statements:
                cur.execute(sql)
            self._migrate_v1_rows(cur)
            conn.commit()
        finally:
            conn.close()

    def _migrate_v1_rows(self, cur) -> None:
        """Copy legacy cache rows into immutable V2 facts without deleting rollback data."""
        if self._is_postgres:
            cur.execute(
                """INSERT INTO research_security_snapshots
                   (snapshot_date,symbol,ts_code,name,exchange,market,industry,list_status,
                    list_date,delist_date,is_hs,provider,origin,effective_at,retrieved_at,
                    schema_version,quality_status,payload)
                   SELECT as_of,symbol,ts_code,name,exchange,market,industry,list_status,
                          list_date,delist_date,is_hs,provider,origin,effective_at,retrieved_at,
                          '2',quality_status,payload FROM research_securities
                   ON CONFLICT(snapshot_date,symbol) DO NOTHING"""
            )
            cur.execute(
                """INSERT INTO research_financial_facts
                   (table_name,symbol,stat_date,pub_date,revision_no,provider,first_seen_as_of,
                    origin,effective_at,retrieved_at,schema_version,quality_status,payload)
                   SELECT table_name,symbol,COALESCE(NULLIF(stat_date,''),'0001-01-01'),
                          COALESCE(NULLIF(pub_date,''),as_of),'legacy',provider,as_of,origin,
                          effective_at,retrieved_at,'2',quality_status,payload
                   FROM research_financial_pit
                   ON CONFLICT(table_name,symbol,stat_date,pub_date,revision_no,provider) DO NOTHING"""
            )
        else:
            cur.execute(
                """INSERT OR IGNORE INTO research_security_snapshots
                   (snapshot_date,symbol,ts_code,name,exchange,market,industry,list_status,
                    list_date,delist_date,is_hs,provider,origin,effective_at,retrieved_at,
                    schema_version,quality_status,payload)
                   SELECT as_of,symbol,ts_code,name,exchange,market,industry,list_status,
                          list_date,delist_date,is_hs,provider,origin,effective_at,retrieved_at,
                          '2',quality_status,payload FROM research_securities"""
            )
            cur.execute(
                """INSERT OR IGNORE INTO research_financial_facts
                   (table_name,symbol,stat_date,pub_date,revision_no,provider,first_seen_as_of,
                    origin,effective_at,retrieved_at,schema_version,quality_status,payload)
                   SELECT table_name,symbol,COALESCE(NULLIF(stat_date,''),'0001-01-01'),
                          COALESCE(NULLIF(pub_date,''),as_of),'legacy',provider,as_of,origin,
                          effective_at,retrieved_at,'2',quality_status,payload
                   FROM research_financial_pit"""
            )

    def start_sync(self, provider: str, capability: str, as_of: str) -> str:
        run_id = uuid.uuid4().hex
        conn = self.connect()
        try:
            conn.execute(
                """INSERT INTO research_sync_runs
                   (run_id,provider,capability,as_of,started_at,status,row_count,quality_status,detail)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_id, provider, capability, as_of, datetime.now().astimezone().isoformat(),
                 "running", 0, "pending", "{}"),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id

    def finish_sync(self, run_id: str, *, status: str, row_count: int,
                    quality_status: str, detail: Optional[dict] = None) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """UPDATE research_sync_runs SET finished_at=?,status=?,row_count=?,
                   quality_status=?,detail=? WHERE run_id=?""",
                (datetime.now().astimezone().isoformat(), status, int(row_count),
                 quality_status, _json(detail or {}), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def upsert_securities(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        provenance = df.attrs.get("provenance", {})
        now = provenance.get("retrieved_at") or datetime.now().astimezone().isoformat()
        rows = []
        for record in df.to_dict("records"):
            symbol = _symbol(record.get("ts_code") or record.get("code") or record.get("symbol"))
            if not symbol:
                continue
            snapshot_date = _iso_date(provenance.get("as_of") or date.today().isoformat())
            rows.append((
                snapshot_date, symbol, str(record.get("ts_code") or ""), _plain(record.get("name")),
                _plain(record.get("exchange")), _plain(record.get("market")),
                _plain(record.get("industry")), _plain(record.get("list_status")),
                _iso_date(record.get("list_date")), _iso_date(record.get("delist_date")),
                _plain(record.get("is_hs")), provenance.get("provider", "unknown"),
                provenance.get("origin", "provider_api"),
                provenance.get("effective_at", date.today().isoformat()), now,
                provenance.get("schema_version", SCHEMA_VERSION),
                provenance.get("quality_status", "ok"),
                _json({k: _plain(v) for k, v in record.items()}),
            ))
        self._executemany(
            """INSERT INTO research_security_snapshots
               (snapshot_date,symbol,ts_code,name,exchange,market,industry,list_status,list_date,
                delist_date,is_hs,provider,origin,effective_at,retrieved_at,schema_version,
                quality_status,payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_date,symbol) DO UPDATE SET
                ts_code=excluded.ts_code,name=excluded.name,exchange=excluded.exchange,
                market=excluded.market,industry=excluded.industry,list_status=excluded.list_status,
                list_date=excluded.list_date,delist_date=excluded.delist_date,is_hs=excluded.is_hs,
                provider=excluded.provider,origin=excluded.origin,effective_at=excluded.effective_at,
                retrieved_at=excluded.retrieved_at,
                schema_version=excluded.schema_version,quality_status=excluded.quality_status,
                payload=excluded.payload""", rows,
        )
        return len(rows)

    def upsert_daily_bars(self, df: pd.DataFrame, *, adjustment: str = "qfq") -> int:
        if df is None or df.empty:
            return 0
        provenance = df.attrs.get("provenance", {})
        now = provenance.get("retrieved_at") or datetime.now().astimezone().isoformat()
        rows = []
        for record in df.to_dict("records"):
            symbol = _symbol(record.get("ts_code") or record.get("code") or record.get("symbol"))
            trade_date = _iso_date(record.get("trade_date") or record.get("date"))
            if not symbol or not trade_date:
                continue
            rows.append((
                symbol, trade_date, adjustment,
                _plain(record.get("open")), _plain(record.get("high")),
                _plain(record.get("low")), _plain(record.get("close")),
                _plain(record.get("volume", record.get("vol"))),
                _plain(record.get("turnover", record.get("amount"))),
                _plain(record.get("turnover_rate")), int(bool(record.get("is_paused", False))),
                int(bool(record.get("is_st", False))), provenance.get("provider", "unknown"),
                provenance.get("origin", "provider_api"), trade_date, now,
                provenance.get("unit", "price/currency/shares"),
                provenance.get("schema_version", SCHEMA_VERSION),
                provenance.get("quality_status", "ok"),
            ))
        self._executemany(
            """INSERT INTO research_daily_bars
               (symbol,trade_date,adjustment,open,high,low,close,volume,amount,turnover_rate,
                is_paused,is_st,provider,origin,effective_at,retrieved_at,unit,schema_version,quality_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,trade_date,adjustment) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                volume=excluded.volume,amount=excluded.amount,turnover_rate=excluded.turnover_rate,
                is_paused=excluded.is_paused,is_st=excluded.is_st,provider=excluded.provider,
                origin=excluded.origin,effective_at=excluded.effective_at,
                retrieved_at=excluded.retrieved_at,unit=excluded.unit,
                schema_version=excluded.schema_version,quality_status=excluded.quality_status""", rows,
        )
        return len(rows)

    def upsert_valuations(self, df: pd.DataFrame, *, as_of: str) -> int:
        if df is None or df.empty:
            return 0
        provenance = df.attrs.get("provenance", {})
        now = provenance.get("retrieved_at") or datetime.now().astimezone().isoformat()
        rows = []
        for record in df.to_dict("records"):
            symbol = _symbol(record.get("code") or record.get("ts_code") or record.get("symbol"))
            trade_date = _iso_date(record.get("trade_date") or as_of)
            if not symbol or not trade_date:
                continue
            rows.append((
                symbol, trade_date, _plain(record.get("market_cap")),
                _plain(record.get("circulating_market_cap")), _plain(record.get("turnover_ratio")),
                _plain(record.get("pe_ratio", record.get("pe_ttm"))),
                _plain(record.get("pe_ratio_lyr")), _plain(record.get("pb_ratio", record.get("pb"))),
                _plain(record.get("ps_ratio", record.get("ps"))),
                _plain(record.get("pcf_ratio", record.get("pcf"))),
                provenance.get("provider", "unknown"), provenance.get("origin", "provider_api"),
                trade_date, now, provenance.get("schema_version", SCHEMA_VERSION),
                provenance.get("quality_status", "ok"),
                _json({k: _plain(v) for k, v in record.items()}),
            ))
        self._executemany(
            """INSERT INTO research_valuations
               (symbol,trade_date,market_cap,circulating_market_cap,turnover_ratio,pe_ttm,pe_lyr,
                pb,ps,pcf,provider,origin,effective_at,retrieved_at,schema_version,quality_status,payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,trade_date) DO UPDATE SET
                market_cap=excluded.market_cap,circulating_market_cap=excluded.circulating_market_cap,
                turnover_ratio=excluded.turnover_ratio,pe_ttm=excluded.pe_ttm,pe_lyr=excluded.pe_lyr,
                pb=excluded.pb,ps=excluded.ps,pcf=excluded.pcf,provider=excluded.provider,
                origin=excluded.origin,effective_at=excluded.effective_at,
                retrieved_at=excluded.retrieved_at,schema_version=excluded.schema_version,
                quality_status=excluded.quality_status,payload=excluded.payload""", rows,
        )
        return len(rows)

    def upsert_financial_pit(self, table: str, df: pd.DataFrame, *, as_of: str) -> int:
        if df is None or df.empty:
            return 0
        provenance = df.attrs.get("provenance", {})
        now = provenance.get("retrieved_at") or datetime.now().astimezone().isoformat()
        cutoff = pd.Timestamp(as_of).normalize()
        rows = []
        for record in df.to_dict("records"):
            symbol = _symbol(record.get("code") or record.get("ts_code") or record.get("symbol"))
            pub_date = _iso_date(record.get("pubDate") or record.get("pub_date"))
            if not symbol:
                continue
            if table != "valuation" and (not pub_date or pd.Timestamp(pub_date) > cutoff):
                continue
            stat_date = _iso_date(record.get("statDate") or record.get("stat_date"))
            if not stat_date:
                continue
            payload = _json({k: _plain(v) for k, v in record.items()})
            revision_no = str(
                record.get("revision_no") or record.get("revisionNo")
                or record.get("update_flag") or hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
            )
            rows.append((
                table, symbol, stat_date, pub_date, revision_no,
                provenance.get("provider", "unknown"), _iso_date(as_of),
                provenance.get("origin", "provider_api"), pub_date, now,
                provenance.get("schema_version", SCHEMA_VERSION),
                provenance.get("quality_status", "ok"), payload,
            ))
        self._executemany(
            """INSERT INTO research_financial_facts
               (table_name,symbol,stat_date,pub_date,revision_no,provider,first_seen_as_of,
                origin,effective_at,retrieved_at,schema_version,quality_status,payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(table_name,symbol,stat_date,pub_date,revision_no,provider) DO UPDATE SET
                retrieved_at=excluded.retrieved_at,quality_status=excluded.quality_status""", rows,
        )
        return len(rows)

    def save_events(self, events: Iterable[dict]) -> int:
        rows = []
        now = datetime.now().astimezone().isoformat()
        for item in events:
            if str(item.get("confirmation_status") or "").lower() != "confirmed":
                continue
            symbol = _symbol(item.get("symbol"))
            effective = _iso_date(item.get("effective_at") or item.get("event_date"))
            if not symbol or not effective:
                continue
            event_id = str(item.get("event_id") or uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{item.get('source')}:{symbol}:{effective}:{item.get('event_type')}:{item.get('title')}"
            ).hex)
            rows.append((
                event_id, symbol, str(item.get("event_type") or "other"),
                _iso_date(item.get("event_date") or effective), effective,
                max(-1.0, min(1.0, float(item.get("direction") or 0))),
                max(0.0, min(1.0, float(item.get("confidence") or 0))),
                max(0.0, min(1.0, float(item.get("materiality") or 0))),
                max(-1.0, min(1.0, float(item.get("surprise") or 0))),
                max(0.0, min(1.0, float(item.get("novelty") or 0))),
                str(item.get("source_family") or "disclosure"),
                str(item.get("source_origin") or item.get("source") or "unknown"),
                str(item.get("document_id") or ""), str(item.get("event_cluster_id") or event_id),
                "confirmed", str(item.get("entity_impact") or "issuer"),
                int(bool(item.get("official"))), str(item.get("title") or "")[:300],
                _json(item.get("original_values") or {}),
                _json(item.get("normalized_values") or {}), _json(item), now,
            ))
        self._executemany(
            """INSERT INTO research_event_records
               (event_id,symbol,event_type,event_date,effective_at,direction,confidence,
                materiality,surprise,novelty,source_family,source_origin,document_id,
                event_cluster_id,confirmation_status,entity_impact,official,title,
                original_values,normalized_values,payload,retrieved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET direction=excluded.direction,
                confidence=excluded.confidence,materiality=excluded.materiality,
                surprise=excluded.surprise,novelty=excluded.novelty,payload=excluded.payload,
                retrieved_at=excluded.retrieved_at""", rows,
        )
        return len(rows)

    def _executemany(self, sql: str, rows: Sequence[tuple]) -> None:
        if not rows:
            return
        conn = self.connect()
        try:
            conn.cursor().executemany(sql, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_trade_days(self, trade_days: Iterable[str], *, provider: str = "zzshare") -> int:
        now = datetime.now().astimezone().isoformat()
        rows = sorted({(_iso_date(day), provider, now) for day in trade_days if _iso_date(day)})
        self._executemany(
            """INSERT INTO research_trade_calendar(trade_date,provider,retrieved_at)
               VALUES (?,?,?) ON CONFLICT(trade_date) DO UPDATE SET
               provider=excluded.provider,retrieved_at=excluded.retrieved_at""",
            rows,
        )
        return len(rows)

    def expected_market_as_of(self, selection_date: str) -> Optional[str]:
        """Return the last locally recorded completed trading day before selection."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(trade_date) FROM research_trade_calendar WHERE trade_date<?",
                (_iso_date(selection_date),),
            )
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None
        finally:
            conn.close()

    def stale_trading_days(self, actual: str, expected: str) -> int:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT COUNT(*) FROM research_trade_calendar
                   WHERE trade_date>? AND trade_date<=?""",
                (_iso_date(actual), _iso_date(expected)),
            )
            row = cur.fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()

    def daily_bar_symbol_count(self, trade_date: str, *, adjustment: str = "qfq",
                               provider: Optional[str] = None) -> int:
        conn = self.connect()
        try:
            cur = conn.cursor()
            sql = (
                "SELECT COUNT(DISTINCT symbol) FROM research_daily_bars "
                "WHERE trade_date=? AND adjustment=? AND close>0 "
                "AND quality_status NOT IN ('unknown_unit','failed')"
            )
            params: List[object] = [_iso_date(trade_date), adjustment]
            if provider:
                sql += " AND provider=?"
                params.append(provider)
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()

    @staticmethod
    def _frame(cur) -> pd.DataFrame:
        rows = cur.fetchall()
        columns = [item[0] for item in cur.description] if cur.description else []
        return pd.DataFrame(rows, columns=columns)

    def load_universe(self, as_of: str) -> pd.DataFrame:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(snapshot_date) FROM research_security_snapshots WHERE snapshot_date<=?",
                (_iso_date(as_of),),
            )
            row = cur.fetchone()
            snapshot = row[0] if row and row[0] else None
            if not snapshot:
                return pd.DataFrame()
            cur.execute(
                """SELECT symbol,name,exchange,market,industry,list_status,list_date,quality_status
                   FROM research_security_snapshots WHERE snapshot_date=?
                   AND (delist_date='' OR delist_date IS NULL OR delist_date>?)""",
                (snapshot, _iso_date(as_of)),
            )
            return self._frame(cur)
        finally:
            conn.close()

    def load_latest_universe(self) -> pd.DataFrame:
        """Return the newest master snapshot for ingestion coverage checks.

        This is deliberately separate from ``load_universe(as_of)``: selectors
        remain point-in-time and must never borrow a later security master.
        """
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT MAX(snapshot_date) FROM research_security_snapshots")
            row = cur.fetchone()
            latest = row[0] if row and row[0] else None
        finally:
            conn.close()
        return self.load_universe(latest) if latest else pd.DataFrame()

    def load_daily_panel(self, as_of: str, *, trading_days: int = 500,
                         adjustment: str = "qfq") -> pd.DataFrame:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT DISTINCT trade_date FROM research_daily_bars
                   WHERE trade_date<=? AND adjustment=? ORDER BY trade_date DESC LIMIT ?""",
                (as_of, adjustment, int(trading_days)),
            )
            dates = [row[0] for row in cur.fetchall()]
            if not dates:
                return pd.DataFrame()
            cur.execute(
                """SELECT symbol,trade_date,open,high,low,close,volume,amount,turnover_rate,
                          is_paused,is_st,provider,quality_status
                   FROM research_daily_bars WHERE trade_date>=? AND trade_date<=? AND adjustment=?
                   ORDER BY symbol,trade_date""",
                (min(dates), as_of, adjustment),
            )
            return self._frame(cur)
        finally:
            conn.close()

    def load_valuations(self, as_of: str) -> pd.DataFrame:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM research_valuations WHERE trade_date<=?", (as_of,))
            row = cur.fetchone()
            if not row or not row[0]:
                return pd.DataFrame()
            cur.execute(
                """SELECT symbol,trade_date,market_cap,circulating_market_cap,turnover_ratio,
                          pe_ttm,pe_lyr,pb,ps,pcf,quality_status
                   FROM research_valuations WHERE trade_date=?""", (row[0],),
            )
            return self._frame(cur)
        finally:
            conn.close()

    def load_financial_pit(self, table: str, as_of: str) -> pd.DataFrame:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT symbol,first_seen_as_of AS as_of,stat_date,pub_date,revision_no,
                          quality_status,payload FROM (
                       SELECT symbol,first_seen_as_of,stat_date,pub_date,revision_no,
                              quality_status,payload,
                              ROW_NUMBER() OVER (
                                  PARTITION BY symbol
                                  ORDER BY stat_date DESC,pub_date DESC,
                                           first_seen_as_of DESC,retrieved_at DESC
                              ) AS row_no
                       FROM research_financial_facts
                       WHERE table_name=? AND pub_date<=? AND first_seen_as_of<=?
                   ) visible WHERE row_no=1""",
                (table, _iso_date(as_of), _iso_date(as_of)),
            )
            frame = self._frame(cur)
            if not frame.empty:
                payloads = frame.pop("payload").map(lambda value: json.loads(value or "{}"))
                expanded = pd.json_normalize(payloads)
                frame = pd.concat([frame.reset_index(drop=True), expanded.reset_index(drop=True)], axis=1)
            return frame
        finally:
            conn.close()

    def load_events(self, as_of: str, *, lookback_days: int = 120) -> pd.DataFrame:
        start = (
            pd.Timestamp(as_of).to_pydatetime()
            - timedelta(days=int(lookback_days) * 2)
        ).date().isoformat()
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT symbol,event_type,event_date,effective_at,direction,confidence,
                          materiality,surprise,novelty,source_origin AS source,official,title
                   FROM research_event_records
                   WHERE confirmation_status='confirmed' AND effective_at>=? AND effective_at<=?""",
                (start, as_of),
            )
            return self._frame(cur)
        finally:
            conn.close()

    def save_selection(self, run: dict, primary: Sequence[dict], reference: Sequence[dict]) -> str:
        run_id = str(run.get("run_id") or uuid.uuid4().hex)
        conn = self.connect()
        try:
            conn.execute(
                """INSERT INTO selection_runs
                   (run_id,selection_date,created_at,status,primary_source,universe_count,
                    eligible_count,final_count,coverage,reference_source,comparison,metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, run["selection_date"], datetime.now().astimezone().isoformat(),
                 run.get("status", "success"), "local_pit", int(run.get("universe_count", 0)),
                 int(run.get("eligible_count", 0)), len(primary), float(run.get("coverage", 0)),
                 "wencai" if reference else None, _json(run.get("comparison", {})),
                 _json(run.get("metadata", {}))),
            )
            rows = []
            for kind, candidates in (("primary", primary), ("wencai_reference", reference)):
                for rank, item in enumerate(candidates, 1):
                    rows.append((
                        run_id, _symbol(item.get("symbol")), kind, rank,
                        _plain(item.get("total_score")), _plain(item.get("fundamental_score")),
                        _plain(item.get("technical_60_score")), _plain(item.get("industry_score")),
                        _plain(item.get("quality_score")), _plain(item.get("correction_120")),
                        _plain(item.get("correction_250")), _plain(item.get("event_correction")),
                        _plain(item.get("data_coverage")), item.get("state"), item.get("industry"),
                        _json(item.get("reasons", [])), _json(item.get("source_labels", [])),
                        _json(item),
                    ))
            conn.cursor().executemany(
                """INSERT INTO selection_candidates
                   (run_id,symbol,candidate_kind,rank_pos,total_score,fundamental_score,
                    technical_60_score,industry_score,quality_score,correction_120,
                    correction_250,event_correction,data_coverage,state,industry,reasons,
                    source_labels,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows,
            )
            conn.commit()
            return run_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def latest_selection(self) -> Optional[dict]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT run_id,selection_date,status,comparison,metadata FROM selection_runs ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                return None
            run_id, selection_date, status, comparison, metadata = row
            cur.execute(
                """SELECT symbol,rank_pos,total_score,fundamental_score,technical_60_score,
                          industry_score,quality_score,correction_120,correction_250,
                          event_correction,data_coverage,state,industry,reasons,source_labels,payload
                   FROM selection_candidates WHERE run_id=? AND candidate_kind='primary'
                   ORDER BY rank_pos""", (run_id,),
            )
            rows = self._frame(cur).to_dict("records")
            for item in rows:
                for key in ("reasons", "source_labels", "payload"):
                    item[key] = json.loads(item.get(key) or ("[]" if key != "payload" else "{}"))
            return {
                "run_id": run_id, "selection_date": selection_date, "status": status,
                "comparison": json.loads(comparison or "{}"), "metadata": json.loads(metadata or "{}"),
                "candidates": rows,
            }
        finally:
            conn.close()
