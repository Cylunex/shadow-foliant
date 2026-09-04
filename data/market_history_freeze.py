"""Bounded, resumable capture of legacy bars for FUTURE selection manifests.

This is a capture of the warehouse now, not proof that a value was known at
its trade date. Never amend an existing manifest or overwrite an observation.
"""
from collections import defaultdict
from datetime import date, datetime
import hashlib

from data.research_store import ResearchStore, SCHEMA_VERSION, _json

_MISSING = """NOT EXISTS (
    SELECT 1 FROM research_market_observations o
    JOIN research_dataset_batches d ON d.dataset_id=o.dataset_id
    WHERE o.dataset_id=b.dataset_id AND o.symbol=b.symbol
      AND o.trade_date=b.trade_date AND o.adjustment=b.adjustment)"""
_COLUMNS = ("symbol", "trade_date", "adjustment", "open", "high", "low", "close",
            "volume", "amount", "turnover_rate", "is_paused", "is_st", "provider",
            "origin", "effective_at", "retrieved_at", "unit", "schema_version", "quality_status")


def _day(value):
    return date.fromisoformat(str(value)).isoformat()


def audit_history(store: ResearchStore, *, through: str) -> dict:
    """Read-only aggregate inventory, no provider calls or price payloads."""
    through = _day(through)
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*),COUNT(DISTINCT trade_date) FROM research_daily_bars "
                    "WHERE trade_date<=? AND adjustment='qfq'", (through,))
        total, days = cur.fetchone()
        cur.execute("SELECT COUNT(*),MIN(b.trade_date),MAX(b.trade_date) "
                    "FROM research_daily_bars b WHERE b.trade_date<=? "
                    "AND b.adjustment='qfq' AND " + _MISSING, (through,))
        missing, first, last = cur.fetchone()
        return {"through": through, "rows": int(total), "trading_days": int(days),
                "unfrozen_rows": int(missing), "unfrozen_first_date": first,
                "unfrozen_last_date": last, "old_manifests_repaired": False}
    finally:
        conn.close()


def freeze_batch(store: ResearchStore, *, through: str, limit: int = 5000) -> dict:
    through = _day(through)
    if not 1 <= limit <= 10000:
        raise ValueError("freeze_batch_limit_must_be_1_to_10000")
    captured = datetime.now().astimezone().isoformat()
    conn = store.connect()
    try:
        cur = conn.cursor()
        if store._is_postgres:
            cur.execute("SET LOCAL lock_timeout='5s'")
            cur.execute("SET LOCAL statement_timeout='60s'")
            # Serialize archive operators, not external API clients.
            cur.execute("SELECT pg_advisory_xact_lock(73461013)")
        else:
            cur.execute("BEGIN IMMEDIATE")  # explicitly injected fixture only
        query = "SELECT " + ",".join("b." + col for col in _COLUMNS)
        query += " FROM research_daily_bars b WHERE b.trade_date<=? AND b.adjustment='qfq' AND " + _MISSING
        query += " ORDER BY b.trade_date,b.symbol LIMIT ?"
        if store._is_postgres:
            query += " FOR UPDATE OF b SKIP LOCKED"
        cur.execute(query, (through, limit))
        rows = [dict(zip(_COLUMNS, row)) for row in cur.fetchall()]
        groups = defaultdict(list)
        for row in rows:
            groups[(row['provider'], row['quality_status'], row['adjustment'])].append(row)
        datasets = []
        for (provider, quality, adjustment), records in groups.items():
            payloads = [_json({**row, "archive_class": "legacy_capture_not_historical_pit",
                               "captured_at": captured}) for row in records]
            digest = hashlib.sha256("\n".join(payloads).encode()).hexdigest()
            dataset = hashlib.sha256(f"legacy-freeze:{provider}:{adjustment}:{digest}".encode()).hexdigest()
            datasets.append(dataset)
            cur.execute("INSERT INTO research_dataset_batches "
                        "(dataset_id,capability,provider,effective_as_of,retrieved_at,schema_version,"
                        "quality_status,row_count,payload_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                        (dataset, 'daily_market', provider, max(r['trade_date'] for r in records),
                         captured, SCHEMA_VERSION, quality, len(records), digest))
            observations = []
            for row, payload in zip(records, payloads):
                oid = hashlib.sha256(f"{dataset}:{row['symbol']}:{row['trade_date']}:{adjustment}".encode()).hexdigest()
                observations.append((oid, dataset, row['symbol'], row['trade_date'], adjustment,
                                     provider, captured, payload))
            cur.executemany("INSERT INTO research_market_observations "
                            "(observation_id,dataset_id,symbol,trade_date,adjustment,provider,retrieved_at,payload) "
                            "VALUES (?,?,?,?,?,?,?,?)", observations)
            cur.executemany("UPDATE research_daily_bars SET dataset_id=? "
                            "WHERE symbol=? AND trade_date=? AND adjustment=?",
                            [(dataset, r['symbol'], r['trade_date'], adjustment) for r in records])
        if datasets:
            fence = hashlib.sha256(_json(sorted(datasets)).encode()).hexdigest()
            # Separate publication clock: never move daily_market freshness back
            # to the dates of this historical capture.
            store._advance_generation(cur, 'market_archive', fence, through)
        conn.commit()
        return {"frozen_rows": len(rows), "datasets": len(datasets), "captured_at": captured,
                "old_manifests_repaired": False, "provider_requests": 0}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
