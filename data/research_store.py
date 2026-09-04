"""Local point-in-time research warehouse used by the primary stock selector.

Only normalized market/research data is stored here.  Credentials, sessions,
portfolio positions and trades are intentionally outside this schema.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence
import uuid

import pandas as pd

import _bootstrap
from db_compat import connect as db_connect


DEFAULT_PATH = _bootstrap.db_path("research_market.db")
SCHEMA_VERSION = "13"


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
        parsed = pd.Timestamp(value)
        if pd.isna(parsed):
            return ""
        return parsed.date().isoformat()
    except Exception:
        return ""


class ResearchStore:
    def __init__(self, path: Optional[str] = None, *, connect_fn: Optional[Callable] = None,
                 is_postgres: Optional[bool] = None, ensure_schema: Optional[bool] = None):
        self.path = str(path or DEFAULT_PATH)
        self._connect_fn = connect_fn or db_connect
        # Explicit test adapters may use an in-memory database. Production never
        # guesses the dialect from a global feature flag: the default connector is PG.
        self._is_postgres = (connect_fn is None) if is_postgres is None else is_postgres
        should_ensure = (
            connect_fn is not None or os.getenv("FOLIANT_RUNTIME_DDL", "false").lower() == "true"
        ) if ensure_schema is None else bool(ensure_schema)
        if should_ensure:
            self.ensure_schema()

    def connect(self):
        return self._connect_fn(self.path)

    def ensure_schema(self) -> None:
        conn = self.connect()
        cur = conn.cursor()
        id_type = "TEXT PRIMARY KEY"
        statements = [
            """CREATE TABLE IF NOT EXISTS research_schema_migrations (
                version TEXT PRIMARY KEY, applied_at TEXT NOT NULL
            )""",
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
            """CREATE TABLE IF NOT EXISTS research_trade_calendar_evidence (
                trade_date TEXT NOT NULL, provider TEXT NOT NULL,
                is_open INTEGER NOT NULL, retrieved_at TEXT NOT NULL,
                PRIMARY KEY(trade_date,provider)
            )""",
            """CREATE TABLE IF NOT EXISTS research_calendar_fetch_runs (
                fetch_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                range_start TEXT NOT NULL, range_end TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, row_count INTEGER NOT NULL,
                open_count INTEGER NOT NULL, closed_count INTEGER NOT NULL,
                quality_status TEXT NOT NULL, detail TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS research_master_snapshot_runs (
                snapshot_id TEXT PRIMARY KEY, snapshot_date TEXT NOT NULL,
                provider TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                observed_count INTEGER NOT NULL, expected_min_count INTEGER NOT NULL,
                exchange_counts TEXT NOT NULL, previous_snapshot_id TEXT,
                quality_status TEXT NOT NULL, published_at TEXT, detail TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS research_security_master_rows (
                snapshot_id TEXT NOT NULL, symbol TEXT NOT NULL, ts_code TEXT NOT NULL,
                name TEXT, exchange TEXT, market TEXT, industry TEXT, list_status TEXT,
                list_date TEXT, delist_date TEXT, is_hs TEXT,
                provider TEXT NOT NULL, origin TEXT NOT NULL, effective_at TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, schema_version TEXT NOT NULL,
                quality_status TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(snapshot_id,symbol)
            )""",
            """CREATE TABLE IF NOT EXISTS research_dataset_batches (
                dataset_id TEXT PRIMARY KEY, capability TEXT NOT NULL,
                provider TEXT NOT NULL, effective_as_of TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, schema_version TEXT NOT NULL,
                quality_status TEXT NOT NULL, row_count INTEGER NOT NULL,
                payload_hash TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS research_dataset_publications (
                capability TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                dataset_id TEXT NOT NULL,
                effective_as_of TEXT,
                published_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS research_dataset_publication_history (
                capability TEXT NOT NULL,
                generation INTEGER NOT NULL,
                dataset_id TEXT NOT NULL,
                effective_as_of TEXT,
                published_at TEXT NOT NULL,
                PRIMARY KEY(capability,generation)
            )""",
            """CREATE TABLE IF NOT EXISTS research_market_observations (
                observation_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
                symbol TEXT NOT NULL, trade_date TEXT NOT NULL, adjustment TEXT NOT NULL,
                provider TEXT NOT NULL, retrieved_at TEXT NOT NULL, payload TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS research_valuation_observations (
                observation_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
                symbol TEXT NOT NULL, requested_as_of TEXT NOT NULL,
                provider_effective_as_of TEXT NOT NULL, provider TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, payload TEXT NOT NULL
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
                quality_status TEXT NOT NULL, dataset_id TEXT,
                PRIMARY KEY(symbol, trade_date, adjustment)
            )""",
            """CREATE TABLE IF NOT EXISTS research_valuations (
                symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                market_cap REAL, circulating_market_cap REAL,
                turnover_ratio REAL, pe_ttm REAL, pe_lyr REAL, pb REAL, ps REAL, pcf REAL,
                dividend_yield REAL,
                provider TEXT NOT NULL, origin TEXT NOT NULL,
                effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                schema_version TEXT NOT NULL, quality_status TEXT NOT NULL,
                requested_as_of TEXT, provider_effective_as_of TEXT, dataset_id TEXT,
                payload TEXT NOT NULL,
                PRIMARY KEY(symbol, trade_date)
            )""",
            """CREATE TABLE IF NOT EXISTS research_fund_flow_daily (
                symbol TEXT NOT NULL, trade_date TEXT NOT NULL, name TEXT,
                close REAL, change_pct REAL, main_net_inflow REAL,
                main_net_inflow_ratio REAL, provider TEXT NOT NULL,
                origin TEXT NOT NULL, effective_at TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, schema_version TEXT NOT NULL,
                quality_status TEXT NOT NULL, payload TEXT NOT NULL,
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
                first_seen_as_of TEXT NOT NULL, first_seen_at TEXT, origin TEXT NOT NULL,
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
            """CREATE TABLE IF NOT EXISTS research_event_revisions (
                revision_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                content_hash TEXT NOT NULL, supersedes_revision_id TEXT,
                symbol TEXT NOT NULL, event_type TEXT NOT NULL,
                event_date TEXT NOT NULL, effective_at TEXT NOT NULL,
                direction REAL NOT NULL, confidence REAL NOT NULL, materiality REAL NOT NULL,
                surprise REAL NOT NULL, novelty REAL NOT NULL,
                source_family TEXT NOT NULL, source_origin TEXT NOT NULL,
                document_id TEXT NOT NULL, event_cluster_id TEXT NOT NULL,
                confirmation_status TEXT NOT NULL, entity_impact TEXT NOT NULL,
                official INTEGER NOT NULL DEFAULT 0, title TEXT,
                original_values TEXT NOT NULL, normalized_values TEXT NOT NULL,
                payload TEXT NOT NULL, first_seen_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                UNIQUE(event_id,content_hash)
            )""",
            """CREATE TABLE IF NOT EXISTS research_artifacts (
                artifact_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                artifact_kind TEXT NOT NULL,
                run_id TEXT NOT NULL,
                formal INTEGER NOT NULL,
                schema_version TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id,artifact_kind)
            )""",
            """CREATE TABLE IF NOT EXISTS research_artifact_annotations (
                annotation_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                annotation_kind TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS selection_runs (
                run_id {id_type}, selection_date TEXT NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL, primary_source TEXT NOT NULL,
                universe_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL,
                final_count INTEGER NOT NULL, coverage REAL NOT NULL,
                reference_source TEXT, comparison TEXT NOT NULL, metadata TEXT NOT NULL,
                publication_status TEXT NOT NULL DEFAULT 'unpublished', published_at TEXT,
                supersedes_run_id TEXT
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
            """CREATE TABLE IF NOT EXISTS selection_input_manifests (
                manifest_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                decision_context TEXT NOT NULL, universe_snapshot_id TEXT NOT NULL,
                market_dataset_ids TEXT NOT NULL, valuation_dataset_ids TEXT NOT NULL,
                financial_revision_set_id TEXT NOT NULL, event_dataset_id TEXT NOT NULL,
                policy_version TEXT NOT NULL, policy_hash TEXT NOT NULL,
                policy_payload TEXT NOT NULL, code_revision TEXT NOT NULL,
                dependency_lock_hash TEXT, strategy_snapshot TEXT,
                publication_generations TEXT,
                schema_version TEXT NOT NULL, created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS selection_artifacts (
                artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL, parent_snapshot_id TEXT,
                rule_version TEXT NOT NULL, policy_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id,artifact_type)
            )""",
            """CREATE TABLE IF NOT EXISTS selection_strategy_runs (
                strategy_run_id TEXT PRIMARY KEY, selection_run_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
                lane TEXT NOT NULL, status TEXT NOT NULL, data_as_of TEXT,
                input_snapshot_id TEXT, policy_version TEXT NOT NULL,
                metadata TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(selection_run_id,strategy_id,strategy_version)
            )""",
            """CREATE TABLE IF NOT EXISTS selection_candidate_nominations (
                nomination_id TEXT PRIMARY KEY, selection_run_id TEXT NOT NULL,
                strategy_run_id TEXT NOT NULL, symbol TEXT NOT NULL, lane TEXT NOT NULL,
                strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
                lane_rank INTEGER NOT NULL, lane_score_raw REAL,
                priority_weight REAL NOT NULL, eligibility TEXT NOT NULL,
                evidence TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(selection_run_id,strategy_id,strategy_version,symbol)
            )""",
            """CREATE TABLE IF NOT EXISTS selection_candidate_outcomes (
                nomination_id TEXT NOT NULL, horizon_days INTEGER NOT NULL,
                entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL,
                return_pct REAL, benchmark_return_pct REAL, max_drawdown_pct REAL,
                outcome_status TEXT NOT NULL, evaluated_at TEXT NOT NULL,
                PRIMARY KEY(nomination_id,horizon_days)
            )""",
            """CREATE TABLE IF NOT EXISTS strategy_policy_versions (
                policy_version TEXT NOT NULL, policy_hash TEXT NOT NULL,
                state TEXT NOT NULL, effective_from TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(policy_version,policy_hash)
            )""",
            """CREATE TABLE IF NOT EXISTS strategy_adjustment_proposals (
                proposal_id TEXT PRIMARY KEY, base_policy_hash TEXT NOT NULL,
                evidence_snapshot_id TEXT NOT NULL, proposal TEXT NOT NULL,
                validation_status TEXT NOT NULL, validation_reason TEXT,
                applied_policy_hash TEXT, created_at TEXT NOT NULL, applied_at TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_research_bars_date ON research_daily_bars(trade_date)",
            "CREATE INDEX IF NOT EXISTS idx_market_observation_identity ON research_market_observations(dataset_id,symbol,trade_date,adjustment)",
            "CREATE INDEX IF NOT EXISTS idx_research_fund_flow_date ON research_fund_flow_daily(trade_date,main_net_inflow)",
            "CREATE INDEX IF NOT EXISTS idx_research_calendar_evidence ON research_trade_calendar_evidence(trade_date,is_open)",
            "CREATE INDEX IF NOT EXISTS idx_research_calendar_fetch ON research_calendar_fetch_runs(provider,range_end,quality_status)",
            "CREATE INDEX IF NOT EXISTS idx_research_master_published ON research_master_snapshot_runs(snapshot_date,published_at)",
            "CREATE INDEX IF NOT EXISTS idx_research_security_snapshot ON research_security_snapshots(snapshot_date,symbol)",
            "CREATE INDEX IF NOT EXISTS idx_research_financial_visible ON research_financial_facts(table_name,first_seen_as_of,pub_date,symbol)",
            "CREATE INDEX IF NOT EXISTS idx_research_finance_asof ON research_financial_pit(as_of, table_name)",
            "CREATE INDEX IF NOT EXISTS idx_research_events_date ON research_events(event_date, symbol)",
            "CREATE INDEX IF NOT EXISTS idx_research_event_records_date ON research_event_records(event_date,symbol,confirmation_status)",
            "CREATE INDEX IF NOT EXISTS idx_research_event_revisions_visible ON research_event_revisions(event_id,retrieved_at)",
            "CREATE INDEX IF NOT EXISTS idx_selection_runs_date ON selection_runs(selection_date, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_selection_nominations_symbol ON selection_candidate_nominations(symbol,created_at)",
            "CREATE INDEX IF NOT EXISTS idx_selection_strategy_runs ON selection_strategy_runs(strategy_id,created_at)",
            "CREATE INDEX IF NOT EXISTS idx_research_publication_history ON research_dataset_publication_history(capability,generation)",
            """CREATE TABLE IF NOT EXISTS research_model_orders (
                order_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,baseline TEXT NOT NULL,
                symbol TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS research_model_portfolios (
                baseline TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS research_model_targets (
                target_id TEXT PRIMARY KEY,baseline TEXT NOT NULL,run_id TEXT NOT NULL,
                state TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS research_experiments (
                trial_id TEXT PRIMARY KEY,hypothesis_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS research_holdout_batches (
                batch_id TEXT PRIMARY KEY,start_date TEXT NOT NULL,end_date TEXT NOT NULL,
                state TEXT NOT NULL,consumed_by TEXT,consumed_at TEXT)""",
        ]
        from data.reliability_store import SCHEMA as reliability_schema
        statements.extend(reliability_schema)
        try:
            for sql in statements:
                cur.execute(sql)
            self._apply_schema_migrations(cur)
            conn.commit()
        finally:
            conn.close()

    def _apply_schema_migrations(self, cur) -> None:
        """Run ordered, idempotent compatibility migrations."""
        cur.execute("INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?) ON CONFLICT(version) DO NOTHING",
                    ("12-research-reliability", datetime.now().astimezone().isoformat()))
        version = "11-research-decision-loop"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            self._add_column(cur, "selection_candidate_outcomes", "mae_pct", "REAL")
            self._add_column(cur, "selection_candidate_outcomes", "metric_version", "TEXT NOT NULL DEFAULT 'legacy-price-v1'")
            cur.execute("INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                        (version, datetime.now().astimezone().isoformat()))
        version = "2-copy-legacy-pit"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            self._migrate_v1_rows(cur)
            cur.execute(
                "INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, datetime.now().astimezone().isoformat()),
            )
        version = "4-reproducible-inputs"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            self._add_column(cur, "research_daily_bars", "dataset_id", "TEXT")
            self._add_column(cur, "research_valuations", "requested_as_of", "TEXT")
            self._add_column(cur, "research_valuations", "provider_effective_as_of", "TEXT")
            self._add_column(cur, "research_valuations", "dataset_id", "TEXT")
            self._add_column(cur, "selection_input_manifests", "dependency_lock_hash", "TEXT")
            cur.execute(
                "INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, datetime.now().astimezone().isoformat()),
            )
        version = "4-financial-availability-time"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            self._add_column(cur, "research_financial_facts", "first_seen_at", "TEXT")
            cur.execute(
                "INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, datetime.now().astimezone().isoformat()),
            )
        version = "6-local-reference-strategies"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            self._add_column(cur, "research_valuations", "dividend_yield", "REAL")
            cur.execute(
                "INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, datetime.now().astimezone().isoformat()),
            )
        version = "7-local-fusion"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            self._add_column(cur, "selection_input_manifests", "strategy_snapshot", "TEXT")
            cur.execute(
                "INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, datetime.now().astimezone().isoformat()),
            )
        version = "09-operational-integrity-v5"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            self._add_column(
                cur, "selection_input_manifests", "publication_generations", "TEXT"
            )
            cur.execute(
                "INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, datetime.now().astimezone().isoformat()),
            )
        # Some early PostgreSQL bootstrap scripts recorded the migration marker
        # after adding the column but before backfilling it. Keep this repair
        # unconditional and idempotent so those rows regain an honest PIT time.
        cur.execute(
            "UPDATE research_financial_facts SET first_seen_at=retrieved_at "
            "WHERE first_seen_at IS NULL OR first_seen_at=''"
        )
        version = "4-manifest-dependency-lock"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            self._add_column(cur, "selection_input_manifests", "dependency_lock_hash", "TEXT")
            cur.execute(
                "INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, datetime.now().astimezone().isoformat()),
            )
        version = "5-event-revisions-and-publication"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            self._add_column(
                cur, "selection_runs", "publication_status",
                "TEXT NOT NULL DEFAULT 'unpublished'",
            )
            self._add_column(cur, "selection_runs", "published_at", "TEXT")
            self._add_column(cur, "selection_runs", "supersedes_run_id", "TEXT")
            now = datetime.now().astimezone().isoformat()
            cur.execute(
                """UPDATE selection_runs SET publication_status='published',published_at=created_at
                   WHERE status='success' AND publication_status='unpublished'
                     AND EXISTS (SELECT 1 FROM selection_input_manifests m
                                 WHERE m.run_id=selection_runs.run_id)
                     AND EXISTS (SELECT 1 FROM selection_artifacts a
                                 WHERE a.run_id=selection_runs.run_id
                                   AND a.artifact_type='formal_top15')
                     AND EXISTS (SELECT 1 FROM selection_artifacts a
                                 WHERE a.run_id=selection_runs.run_id
                                   AND a.artifact_type='formal_top5')"""
            )
            cur.execute(
                "INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, now),
            )
        # V5 introduced immutable master snapshots.  Existing PostgreSQL installs
        # already have a validated legacy security snapshot, so publish the newest
        # sufficiently complete one instead of leaving the selector with an empty
        # universe until the next external master refresh.
        self._promote_legacy_master(cur)
        version = "6-publish-legacy-master"
        cur.execute("SELECT 1 FROM research_schema_migrations WHERE version=?", (version,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO research_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, datetime.now().astimezone().isoformat()),
            )

    def _promote_legacy_master(self, cur) -> Optional[str]:
        """Idempotently publish the newest complete V2 master as a V5 snapshot."""
        cur.execute(
            """SELECT snapshot_id FROM research_master_snapshot_runs
               WHERE quality_status='ok' AND published_at IS NOT NULL LIMIT 1"""
        )
        if cur.fetchone():
            return None
        minimum_rows = max(1, int(os.getenv("RESEARCH_MIN_UNIVERSE_ROWS", "3000")))
        cur.execute(
            """SELECT snapshot_date,COUNT(*),MAX(retrieved_at)
               FROM research_security_snapshots GROUP BY snapshot_date
               HAVING COUNT(*)>=? ORDER BY snapshot_date DESC LIMIT 1""",
            (minimum_rows,),
        )
        legacy = cur.fetchone()
        if not legacy:
            return None
        snapshot_date, observed_count, retrieved_at = legacy
        snapshot_date = str(snapshot_date)
        observed_count = int(observed_count or 0)
        retrieved_at = str(retrieved_at or datetime.now().astimezone().isoformat())
        snapshot_id = hashlib.sha256(
            f"legacy-master:{snapshot_date}:{observed_count}".encode("utf-8")
        ).hexdigest()
        cur.execute(
            """SELECT COALESCE(NULLIF(exchange,''),'unknown'),COUNT(*)
               FROM research_security_snapshots WHERE snapshot_date=?
               GROUP BY COALESCE(NULLIF(exchange,''),'unknown')""",
            (snapshot_date,),
        )
        exchange_counts = {str(row[0]): int(row[1] or 0) for row in cur.fetchall()}
        cur.execute(
            """INSERT INTO research_master_snapshot_runs
               (snapshot_id,snapshot_date,provider,retrieved_at,observed_count,
                expected_min_count,exchange_counts,previous_snapshot_id,quality_status,
                published_at,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_id) DO NOTHING""",
            (snapshot_id, snapshot_date, "legacy-migration", retrieved_at, observed_count,
             minimum_rows, _json(exchange_counts), None, "ok", retrieved_at,
             _json({"source": "research_security_snapshots", "migration": "v6"})),
        )
        cur.execute(
            """INSERT INTO research_security_master_rows
               (snapshot_id,symbol,ts_code,name,exchange,market,industry,list_status,
                list_date,delist_date,is_hs,provider,origin,effective_at,retrieved_at,
                schema_version,quality_status,payload)
               SELECT ?,symbol,ts_code,name,exchange,market,industry,list_status,
                      list_date,delist_date,is_hs,provider,origin,effective_at,retrieved_at,
                      schema_version,quality_status,payload
               FROM research_security_snapshots WHERE snapshot_date=?
               ON CONFLICT(snapshot_id,symbol) DO NOTHING""",
            (snapshot_id, snapshot_date),
        )
        return snapshot_id

    def _add_column(self, cur, table: str, column: str, definition: str) -> None:
        if self._is_postgres:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
            return
        cur.execute(f"PRAGMA table_info({table})")
        if column not in {str(row[1]) for row in cur.fetchall()}:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    @staticmethod
    def _advance_generation(cur, capability: str, dataset_id: str,
                            effective_as_of: Optional[str] = None) -> int:
        """Advance one publication fence in the caller's data transaction."""
        now = datetime.now().astimezone().isoformat()
        cur.execute(
            """SELECT generation,dataset_id,effective_as_of
               FROM research_dataset_publications WHERE capability=?""",
            (str(capability),),
        )
        row = cur.fetchone()
        if row and str(row[1]) == str(dataset_id) and str(row[2] or "") == str(
            effective_as_of or ""
        ):
            return int(row[0] or 0)
        generation = int(row[0] or 0) + 1 if row else 1
        cur.execute(
            """INSERT INTO research_dataset_publications
               (capability,generation,dataset_id,effective_as_of,published_at)
               VALUES (?,?,?,?,?) ON CONFLICT(capability) DO UPDATE SET
                 generation=excluded.generation,dataset_id=excluded.dataset_id,
                 effective_as_of=excluded.effective_as_of,published_at=excluded.published_at""",
            (str(capability), generation, str(dataset_id), effective_as_of, now),
        )
        cur.execute(
            """INSERT INTO research_dataset_publication_history
               (capability,generation,dataset_id,effective_as_of,published_at)
               VALUES (?,?,?,?,?) ON CONFLICT(capability,generation) DO NOTHING""",
            (str(capability), generation, str(dataset_id), effective_as_of, now),
        )
        return generation

    def generation_vector(self, capabilities: Optional[Iterable[str]] = None) -> dict[str, int]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            requested = sorted({str(value) for value in (capabilities or []) if value})
            if requested:
                placeholders = ",".join(["?"] * len(requested))
                cur.execute(
                    f"SELECT capability,generation FROM research_dataset_publications "
                    f"WHERE capability IN ({placeholders}) ORDER BY capability",
                    tuple(requested),
                )
            else:
                cur.execute(
                    "SELECT capability,generation FROM research_dataset_publications ORDER BY capability"
                )
            found = {str(row[0]): int(row[1] or 0) for row in cur.fetchall()}
            return {name: found.get(name, 0) for name in requested} if requested else found
        finally:
            conn.close()

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

    def completed_sync(self, capability: str, as_of: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT 1 FROM research_sync_runs
                   WHERE capability=? AND as_of=? AND status='success' AND quality_status='ok'
                   ORDER BY finished_at DESC LIMIT 1""",
                (str(capability), _iso_date(as_of)),
            )
            return bool(cur.fetchone())
        finally:
            conn.close()

    def _security_rows(self, df: pd.DataFrame, snapshot_id: str) -> list[tuple]:
        if df is None or df.empty:
            return []
        provenance = df.attrs.get("provenance", {})
        now = provenance.get("retrieved_at") or datetime.now().astimezone().isoformat()
        rows = []
        for record in df.to_dict("records"):
            symbol = _symbol(record.get("ts_code") or record.get("code") or record.get("symbol"))
            if not symbol:
                continue
            rows.append((
                snapshot_id, symbol, str(record.get("ts_code") or ""), _plain(record.get("name")),
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
        return rows

    def publish_security_master(self, df: pd.DataFrame, *, minimum_rows: int,
                                validate_change: bool = True) -> dict:
        """Stage, validate, and atomically publish one immutable master snapshot."""
        provenance = (df.attrs.get("provenance", {}) if df is not None else {})
        snapshot_id = uuid.uuid4().hex
        snapshot_date = _iso_date(provenance.get("as_of") or date.today().isoformat())
        retrieved_at = provenance.get("retrieved_at") or datetime.now().astimezone().isoformat()
        rows = self._security_rows(df, snapshot_id)
        exchange_counts: Dict[str, int] = {}
        for row in rows:
            exchange = str(row[4] or "unknown").strip().upper() or "unknown"
            exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT snapshot_id,observed_count,exchange_counts
                   FROM research_master_snapshot_runs
                   WHERE published_at IS NOT NULL AND snapshot_date<=?
                   ORDER BY snapshot_date DESC,published_at DESC LIMIT 1""",
                (snapshot_date,),
            )
            previous = cur.fetchone()
            previous_id = str(previous[0]) if previous else None
            reasons = []
            if len(rows) < int(minimum_rows):
                reasons.append("absolute_count_below_minimum")
            lifecycle_complete = (df.attrs.get("lifecycle_complete")
                                  if df is not None else None)
            if lifecycle_complete is False:
                reasons.append("lifecycle_statuses_incomplete")
            if len({row[1] for row in rows}) != len(rows):
                reasons.append("duplicate_symbols")
            if validate_change and previous:
                previous_count = int(previous[1] or 0)
                if previous_count and abs(len(rows) - previous_count) / previous_count > 0.20:
                    reasons.append("total_count_change_exceeds_20pct")
                previous_exchanges = json.loads(previous[2] or "{}")
                for exchange, old_count in previous_exchanges.items():
                    if int(old_count or 0) >= 50 and exchange_counts.get(exchange, 0) < int(old_count) * 0.70:
                        reasons.append(f"exchange_drop:{exchange}")
            quality = "ok" if not reasons else "incomplete"
            cur.execute(
                """INSERT INTO research_master_snapshot_runs
                   (snapshot_id,snapshot_date,provider,retrieved_at,observed_count,
                    expected_min_count,exchange_counts,previous_snapshot_id,quality_status,
                    published_at,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, snapshot_date, provenance.get("provider", "unknown"),
                 retrieved_at, len(rows), int(minimum_rows), _json(exchange_counts),
                 previous_id, quality, retrieved_at if quality == "ok" else None,
                 _json({
                     "reasons": reasons,
                     "requested_list_statuses": list(
                         (df.attrs.get("requested_list_statuses") if df is not None else []) or []
                     ),
                     "available_list_statuses": list(
                         (df.attrs.get("available_list_statuses") if df is not None else []) or []
                     ),
                     "lifecycle_complete": lifecycle_complete,
                 })),
            )
            cur.executemany(
                """INSERT INTO research_security_master_rows
                   (snapshot_id,symbol,ts_code,name,exchange,market,industry,list_status,
                    list_date,delist_date,is_hs,provider,origin,effective_at,retrieved_at,
                    schema_version,quality_status,payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            if quality == "ok":
                self._advance_generation(cur, "security_master", snapshot_id, snapshot_date)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "snapshot_id": snapshot_id, "snapshot_date": snapshot_date,
            "rows": len(rows), "quality_status": quality,
            "published": quality == "ok", "exchange_counts": exchange_counts,
            "reasons": reasons,
        }

    def upsert_securities(self, df: pd.DataFrame) -> int:
        """Compatibility helper for trusted fixtures and manual imports."""
        result = self.publish_security_master(df, minimum_rows=1, validate_change=False)
        if not result["published"]:
            return 0
        # Keep the V2 table populated for rollback compatibility only.
        provenance = df.attrs.get("provenance", {})
        snapshot_date = _iso_date(provenance.get("as_of") or date.today().isoformat())
        legacy_rows = [
            (snapshot_date,) + tuple(row[1:]) for row in self._security_rows(df, result["snapshot_id"])
        ]
        self._executemany(
            """INSERT INTO research_security_snapshots
               (snapshot_date,symbol,ts_code,name,exchange,market,industry,list_status,list_date,
                delist_date,is_hs,provider,origin,effective_at,retrieved_at,schema_version,
                quality_status,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_date,symbol) DO NOTHING""",
            legacy_rows,
        )
        return int(result["rows"])

    def upsert_daily_bars(self, df: pd.DataFrame, *, adjustment: str = "qfq") -> int:
        if df is None or df.empty:
            return 0
        provenance = df.attrs.get("provenance", {})
        now = provenance.get("retrieved_at") or datetime.now().astimezone().isoformat()
        rows = []
        observations = []
        payloads = []
        for record in df.to_dict("records"):
            symbol = _symbol(record.get("ts_code") or record.get("code") or record.get("symbol"))
            trade_date = _iso_date(record.get("trade_date") or record.get("date"))
            if not symbol or not trade_date:
                continue
            payload = _json({k: _plain(v) for k, v in record.items()})
            payloads.append(payload)
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
                None,
            ))
        if not rows:
            return 0
        payload_hash = hashlib.sha256("\n".join(sorted(payloads)).encode("utf-8")).hexdigest()
        dataset_id = hashlib.sha256(
            f"market:{provenance.get('provider','unknown')}:{now}:{payload_hash}".encode("utf-8")
        ).hexdigest()
        rows = [row[:-1] + (dataset_id,) for row in rows]
        for row, payload in zip(rows, payloads):
            observation_id = hashlib.sha256(
                f"{dataset_id}:{row[0]}:{row[1]}:{adjustment}".encode("utf-8")
            ).hexdigest()
            observations.append((
                observation_id, dataset_id, row[0], row[1], adjustment,
                provenance.get("provider", "unknown"), now, payload,
            ))
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO research_dataset_batches
                   (dataset_id,capability,provider,effective_as_of,retrieved_at,
                    schema_version,quality_status,row_count,payload_hash)
                   VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(dataset_id) DO NOTHING""",
                (dataset_id, "daily_market", provenance.get("provider", "unknown"),
                 max(row[1] for row in rows), now,
                 provenance.get("schema_version", SCHEMA_VERSION),
                 provenance.get("quality_status", "ok"), len(rows), payload_hash),
            )
            cur.executemany(
                """INSERT INTO research_market_observations
                   (observation_id,dataset_id,symbol,trade_date,adjustment,provider,
                    retrieved_at,payload) VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(observation_id) DO NOTHING""",
                observations,
            )
            cur.executemany(
            """INSERT INTO research_daily_bars
               (symbol,trade_date,adjustment,open,high,low,close,volume,amount,turnover_rate,
                is_paused,is_st,provider,origin,effective_at,retrieved_at,unit,schema_version,
                quality_status,dataset_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,trade_date,adjustment) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                volume=excluded.volume,amount=excluded.amount,turnover_rate=excluded.turnover_rate,
                is_paused=excluded.is_paused,is_st=excluded.is_st,provider=excluded.provider,
                origin=excluded.origin,effective_at=excluded.effective_at,
                retrieved_at=excluded.retrieved_at,unit=excluded.unit,
                schema_version=excluded.schema_version,quality_status=excluded.quality_status,
                dataset_id=excluded.dataset_id""", rows,
            )
            self._advance_generation(
                cur, "daily_market", dataset_id, max(row[1] for row in rows)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(rows)

    def upsert_valuations(self, df: pd.DataFrame, *, as_of: str) -> int:
        if df is None or df.empty:
            return 0
        provenance = df.attrs.get("provenance", {})
        now = provenance.get("retrieved_at") or datetime.now().astimezone().isoformat()
        requested_as_of = _iso_date(as_of)
        rows = []
        payloads = []
        for record in df.to_dict("records"):
            symbol = _symbol(record.get("code") or record.get("ts_code") or record.get("symbol"))
            provider_effective = _iso_date(
                record.get("provider_effective_as_of") or record.get("trade_date")
                or record.get("tradeDate") or record.get("date")
            )
            if not symbol or not provider_effective or provider_effective != requested_as_of:
                continue
            payload = _json({k: _plain(v) for k, v in record.items()})
            payloads.append(payload)
            rows.append((
                symbol, provider_effective, _plain(record.get("market_cap")),
                _plain(record.get("circulating_market_cap")), _plain(record.get("turnover_ratio")),
                _plain(record.get("pe_ratio", record.get("pe_ttm"))),
                _plain(record.get("pe_ratio_lyr", record.get("pe_lyr"))), _plain(record.get("pb_ratio", record.get("pb"))),
                _plain(record.get("ps_ratio", record.get("ps"))),
                _plain(record.get("pcf_ratio", record.get("pcf"))),
                _plain(next((record.get(key) for key in (
                    "dividend_yield", "dividend_yield_ratio", "dv_ratio", "dividend_ratio",
                ) if record.get(key) is not None), None)),
                provenance.get("provider", "unknown"), provenance.get("origin", "provider_api"),
                provider_effective, now, provenance.get("schema_version", SCHEMA_VERSION),
                (record.get("quality_status", "partial") if record.get("valuation_contract") == "composite-v1"
                 else provenance.get("quality_status", "ok")),
                requested_as_of, provider_effective, None, payload,
            ))
        if not rows:
            return 0
        payload_hash = hashlib.sha256("\n".join(sorted(payloads)).encode("utf-8")).hexdigest()
        dataset_id = hashlib.sha256(
            f"valuation:{provenance.get('provider','unknown')}:{now}:{payload_hash}".encode("utf-8")
        ).hexdigest()
        rows = [row[:-2] + (dataset_id, row[-1]) for row in rows]
        observations = []
        for row, payload in zip(rows, payloads):
            observation_id = hashlib.sha256(
                f"{dataset_id}:{row[0]}:{requested_as_of}:{row[1]}".encode("utf-8")
            ).hexdigest()
            observations.append((
                observation_id, dataset_id, row[0], requested_as_of, row[1],
                provenance.get("provider", "unknown"), now, payload,
            ))
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO research_dataset_batches
                   (dataset_id,capability,provider,effective_as_of,retrieved_at,
                    schema_version,quality_status,row_count,payload_hash)
                   VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(dataset_id) DO NOTHING""",
                (dataset_id, "valuation", provenance.get("provider", "unknown"),
                 requested_as_of, now, provenance.get("schema_version", SCHEMA_VERSION),
                 provenance.get("quality_status", "ok"), len(rows), payload_hash),
            )
            cur.executemany(
                """INSERT INTO research_valuation_observations
                   (observation_id,dataset_id,symbol,requested_as_of,
                    provider_effective_as_of,provider,retrieved_at,payload)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(observation_id) DO NOTHING""",
                observations,
            )
            cur.executemany(
            """INSERT INTO research_valuations
               (symbol,trade_date,market_cap,circulating_market_cap,turnover_ratio,pe_ttm,pe_lyr,
                pb,ps,pcf,dividend_yield,provider,origin,effective_at,retrieved_at,schema_version,quality_status,
                requested_as_of,provider_effective_as_of,dataset_id,payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,trade_date) DO UPDATE SET
                market_cap=excluded.market_cap,circulating_market_cap=excluded.circulating_market_cap,
                turnover_ratio=excluded.turnover_ratio,pe_ttm=excluded.pe_ttm,pe_lyr=excluded.pe_lyr,
                pb=excluded.pb,ps=excluded.ps,pcf=excluded.pcf,
                dividend_yield=excluded.dividend_yield,provider=excluded.provider,
                origin=excluded.origin,effective_at=excluded.effective_at,
                retrieved_at=excluded.retrieved_at,schema_version=excluded.schema_version,
                quality_status=excluded.quality_status,requested_as_of=excluded.requested_as_of,
                provider_effective_as_of=excluded.provider_effective_as_of,
                dataset_id=excluded.dataset_id,payload=excluded.payload""", rows,
            )
            self._advance_generation(cur, "valuation", dataset_id, requested_as_of)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
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
                provenance.get("provider", "unknown"), _iso_date(as_of), now,
                provenance.get("origin", "provider_api"), pub_date, now,
                provenance.get("schema_version", SCHEMA_VERSION),
                provenance.get("quality_status", "ok"), payload,
            ))
        if not rows:
            return 0
        dataset_id = hashlib.sha256(
            (str(table) + "\n" + "\n".join(sorted(str(row[-1]) for row in rows))).encode("utf-8")
        ).hexdigest()
        conn = self.connect()
        try:
            cur = conn.cursor()
            # Revisions are append-only impacts, not historical payload rewrites.
            # Dataset-level dependencies are explicit until finer lineage exists.
            for fact in rows:
                from data.acquisition_evidence import fact_observation
                payload_record = json.loads(fact[-1])
                for field in ("net_profit", "netProfit", "np_parent_company_owners", "operating_cash_flow", "roe", "ROE", "epsTTM"):
                    if field not in payload_record or payload_record[field] is None:
                        continue
                    observation = fact_observation(symbol=fact[1], field=field, value=payload_record[field], period=fact[2],
                        published_at=fact[3], first_seen_at=fact[7], provider=fact[5], revision=fact[4],
                        unit=provenance.get("unit", "unknown"), currency=provenance.get("currency", "unknown"),
                        basis=provenance.get("basis", "unknown"))
                    cur.execute("INSERT INTO research_reliability_records VALUES (?,?,?,?,?,?) "
                                "ON CONFLICT(kind,object_id,owner_id,revision) DO NOTHING",
                                ("fact_observation", observation["object_id"], "research", 1, _json(observation), now))
                cur.execute("SELECT payload FROM research_financial_facts WHERE table_name=? AND symbol=? "
                            "AND stat_date=? AND provider=? ORDER BY retrieved_at DESC LIMIT 1",
                            (fact[0], fact[1], fact[2], fact[5]))
                old = cur.fetchone()
                if old and str(old[0]) != fact[-1]:
                    from data.acquisition_evidence import revision_impact
                    impact = revision_impact(json.loads(old[0]), json.loads(fact[-1]),
                        [{"kind": "financial_dataset", "symbol": fact[1], "granularity": "dataset"}])
                    impact.update(symbol=fact[1], table=table, stat_date=fact[2], new_dataset_id=dataset_id,
                                  new_values=json.loads(fact[-1]),
                                  old_hash=hashlib.sha256(str(old[0]).encode()).hexdigest())
                    identity = hashlib.sha256(_json(impact).encode()).hexdigest()
                    cur.execute("INSERT INTO research_reliability_records VALUES (?,?,?,?,?,?) "
                                "ON CONFLICT(kind,object_id,owner_id,revision) DO NOTHING",
                                ("revision_impact", identity, "research", 1, _json(impact), now))
            cur.executemany(
                """INSERT INTO research_financial_facts
               (table_name,symbol,stat_date,pub_date,revision_no,provider,first_seen_as_of,
                first_seen_at,origin,effective_at,retrieved_at,schema_version,quality_status,payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(table_name,symbol,stat_date,pub_date,revision_no,provider) DO UPDATE SET
                first_seen_at=COALESCE(NULLIF(research_financial_facts.first_seen_at,''),
                                       excluded.first_seen_at),
                retrieved_at=excluded.retrieved_at,
                quality_status=excluded.quality_status""",
                rows,
            )
            self._advance_generation(cur, "financial_pit", dataset_id, _iso_date(as_of))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(rows)

    def upsert_fund_flow_daily(self, df: pd.DataFrame, *, trade_date: str) -> int:
        """Persist verified whole-market main-force flow for one completed trading day."""
        if df is None or df.empty:
            return 0
        effective_date = _iso_date(trade_date)
        provenance = df.attrs.get("provenance", {})
        now = provenance.get("retrieved_at") or datetime.now().astimezone().isoformat()
        rows = []
        for record in df.to_dict("records"):
            symbol = _symbol(record.get("symbol") or record.get("code"))
            row_date = _iso_date(record.get("trade_date") or effective_date)
            if not symbol or row_date != effective_date:
                continue
            rows.append((
                symbol, effective_date, _plain(record.get("name")),
                _plain(record.get("close")), _plain(record.get("change_pct")),
                _plain(record.get("main_net_inflow")),
                _plain(record.get("main_net_inflow_ratio")),
                provenance.get("provider", "unknown"),
                provenance.get("origin", "provider_api"), effective_date, now,
                provenance.get("schema_version", SCHEMA_VERSION),
                provenance.get("quality_status", "ok"),
                _json({key: _plain(value) for key, value in record.items()}),
            ))
        if not rows:
            return 0
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.executemany(
                """INSERT INTO research_fund_flow_daily
                   (symbol,trade_date,name,close,change_pct,main_net_inflow,
                    main_net_inflow_ratio,provider,origin,effective_at,retrieved_at,
                    schema_version,quality_status,payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,trade_date) DO UPDATE SET
                    name=excluded.name,close=excluded.close,change_pct=excluded.change_pct,
                    main_net_inflow=excluded.main_net_inflow,
                    main_net_inflow_ratio=excluded.main_net_inflow_ratio,
                    provider=excluded.provider,origin=excluded.origin,
                    effective_at=excluded.effective_at,retrieved_at=excluded.retrieved_at,
                    schema_version=excluded.schema_version,
                    quality_status=excluded.quality_status,payload=excluded.payload""",
                rows,
            )
            dataset_id = hashlib.sha256(_json(rows).encode("utf-8")).hexdigest()
            self._advance_generation(cur, "fund_flow", dataset_id, effective_date)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(rows)

    def save_events(self, events: Iterable[dict]) -> int:
        rows: list[tuple] = []
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
        if not rows:
            return 0
        conn = self.connect()
        try:
            cur = conn.cursor()
            revision_added = False
            for row in rows:
                event_id = str(row[0])
                content = {
                    "symbol": row[1], "event_type": row[2], "event_date": row[3],
                    "effective_at": row[4], "direction": row[5], "confidence": row[6],
                    "materiality": row[7], "surprise": row[8], "novelty": row[9],
                    "source_family": row[10], "source_origin": row[11],
                    "document_id": row[12], "event_cluster_id": row[13],
                    "confirmation_status": row[14], "entity_impact": row[15],
                    "official": row[16], "title": row[17],
                    "original_values": json.loads(row[18]),
                    "normalized_values": json.loads(row[19]), "payload": json.loads(row[20]),
                }
                content_hash = hashlib.sha256(_json(content).encode("utf-8")).hexdigest()
                revision_id = hashlib.sha256(
                    f"{event_id}:{content_hash}".encode("utf-8")
                ).hexdigest()
                cur.execute(
                    """SELECT revision_id FROM research_event_revisions
                       WHERE event_id=? ORDER BY first_seen_at DESC LIMIT 1""",
                    (event_id,),
                )
                previous = cur.fetchone()
                supersedes = str(previous[0]) if previous and previous[0] else None
                cur.execute(
                    """INSERT INTO research_event_revisions
                       (revision_id,event_id,content_hash,supersedes_revision_id,symbol,event_type,
                        event_date,effective_at,direction,confidence,materiality,surprise,novelty,
                        source_family,source_origin,document_id,event_cluster_id,
                        confirmation_status,entity_impact,official,title,original_values,
                        normalized_values,payload,first_seen_at,retrieved_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(event_id,content_hash) DO NOTHING""",
                    (revision_id, event_id, content_hash, supersedes, *row[1:-1], now, row[-1]),
                )
                revision_added = revision_added or bool(cur.rowcount)
                cur.execute(
                    """INSERT INTO research_event_records
                       (event_id,symbol,event_type,event_date,effective_at,direction,confidence,
                        materiality,surprise,novelty,source_family,source_origin,document_id,
                        event_cluster_id,confirmation_status,entity_impact,official,title,
                        original_values,normalized_values,payload,retrieved_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(event_id) DO UPDATE SET direction=excluded.direction,
                        confidence=excluded.confidence,materiality=excluded.materiality,
                        surprise=excluded.surprise,novelty=excluded.novelty,
                        source_family=excluded.source_family,
                        source_origin=excluded.source_origin,document_id=excluded.document_id,
                        event_cluster_id=excluded.event_cluster_id,
                        confirmation_status=excluded.confirmation_status,
                        entity_impact=excluded.entity_impact,official=excluded.official,
                        title=excluded.title,original_values=excluded.original_values,
                        normalized_values=excluded.normalized_values,payload=excluded.payload,
                        retrieved_at=excluded.retrieved_at""",
                    row,
                )
            if revision_added:
                max_effective = max(str(row[4]) for row in rows)
                cur.execute(
                    """SELECT revision_id,content_hash FROM (
                         SELECT revision_id,content_hash,event_id,confirmation_status,
                           ROW_NUMBER() OVER (
                             PARTITION BY event_id ORDER BY retrieved_at DESC,revision_id DESC
                           ) AS row_no
                         FROM research_event_revisions WHERE effective_at<=?
                       ) visible WHERE row_no=1 AND confirmation_status='confirmed'
                       ORDER BY revision_id""",
                    (max_effective,),
                )
                event_dataset = hashlib.sha256(
                    "\n".join(f"{row[0]}:{row[1]}" for row in cur.fetchall()).encode("utf-8")
                ).hexdigest()
                self._advance_generation(cur, "events", event_dataset, max_effective)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
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

    def upsert_calendar_evidence(self, evidence: Iterable[tuple[str, bool]], *,
                                 provider: str) -> int:
        """Persist one provider's explicit open/closed evidence for calendar consensus."""
        now = datetime.now().astimezone().isoformat()
        rows = sorted({
            (_iso_date(day), str(provider), int(bool(is_open)), now)
            for day, is_open in evidence if _iso_date(day)
        })
        self._executemany(
            """INSERT INTO research_trade_calendar_evidence
               (trade_date,provider,is_open,retrieved_at) VALUES (?,?,?,?)
               ON CONFLICT(trade_date,provider) DO UPDATE SET
               is_open=excluded.is_open,retrieved_at=excluded.retrieved_at""",
            rows,
        )
        return len(rows)

    def replace_calendar_evidence(self, evidence: Sequence[tuple[str, bool]], *,
                                  provider: str, start_date: str, end_date: str) -> int:
        """Replace one bounded provider range; missing rows remain unknown."""
        now = datetime.now().astimezone().isoformat()
        rows = sorted({
            (_iso_date(day), str(provider), int(bool(is_open)), now)
            for day, is_open in evidence if _iso_date(day)
        })
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """DELETE FROM research_trade_calendar_evidence
                   WHERE provider=? AND trade_date>=? AND trade_date<=?""",
                (str(provider), _iso_date(start_date), _iso_date(end_date)),
            )
            if rows:
                cur.executemany(
                    """INSERT INTO research_trade_calendar_evidence
                       (trade_date,provider,is_open,retrieved_at) VALUES (?,?,?,?)""",
                    rows,
                )
            calendar_dataset = hashlib.sha256(
                _json({"provider": provider, "start": _iso_date(start_date),
                       "end": _iso_date(end_date), "rows": rows}).encode("utf-8")
            ).hexdigest()
            self._advance_generation(
                cur, "trade_calendar", calendar_dataset, _iso_date(end_date)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(rows)

    def record_calendar_fetch(self, provider: str, start_date: str, end_date: str,
                              evidence: Sequence[tuple[str, bool]], *,
                              quality_status: str, detail: Optional[dict] = None) -> str:
        fetch_id = uuid.uuid4().hex
        now = datetime.now().astimezone().isoformat()
        opened = sum(1 for _, state in evidence if state)
        closed = sum(1 for _, state in evidence if not state)
        conn = self.connect()
        try:
            conn.execute(
                """INSERT INTO research_calendar_fetch_runs
                   (fetch_id,provider,range_start,range_end,retrieved_at,row_count,
                    open_count,closed_count,quality_status,detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (fetch_id, str(provider), _iso_date(start_date), _iso_date(end_date), now,
                 len(evidence), opened, closed, quality_status, _json(detail or {})),
            )
            conn.commit()
        finally:
            conn.close()
        return fetch_id

    def calendar_consensus(self, cutoff: str, *, inclusive: bool = False) -> dict:
        """Return two-source calendar coverage and the latest unanimously open day."""
        cutoff = _iso_date(cutoff)
        recent_start = (pd.Timestamp(cutoff).date() - timedelta(days=14)).isoformat()
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT provider,MAX(range_end) FROM research_calendar_fetch_runs
                   WHERE quality_status='ok' GROUP BY provider"""
            )
            provider_coverage = {str(row[0]): str(row[1]) for row in cur.fetchall()}
            covered = sorted(
                provider for provider, through in provider_coverage.items()
                if through and through >= cutoff
            )
            operator = "<=" if inclusive else "<"
            cur.execute(
                f"""SELECT trade_date,COUNT(DISTINCT provider),MIN(is_open),MAX(is_open)
                    FROM research_trade_calendar_evidence
                    WHERE trade_date{operator}?
                    GROUP BY trade_date ORDER BY trade_date DESC""",
                (cutoff,),
            )
            latest_open = None
            disagreement_dates: List[str] = []
            for trade_date, provider_count, minimum, maximum in cur.fetchall():
                if int(provider_count or 0) < 2:
                    continue
                if int(minimum) != int(maximum):
                    if str(trade_date) >= recent_start:
                        disagreement_dates.append(str(trade_date))
                    continue
                if latest_open is None and int(minimum) == 1:
                    latest_open = str(trade_date)
            return {
                "ready": len(covered) >= 2 and not disagreement_dates,
                "provider_count": len(provider_coverage),
                "covered_provider_count": len(covered),
                "coverage_through_date": min(
                    (provider_coverage[p] for p in covered), default=None
                ),
                "latest_confirmed_open_date": latest_open,
                "disagreement_count": len(disagreement_dates),
            }
        finally:
            conn.close()

    def expected_market_as_of(self, selection_date: str, *, inclusive: bool = False) -> Optional[str]:
        """Return the latest independently confirmed completed trading day."""
        consensus = self.calendar_consensus(selection_date, inclusive=inclusive)
        if not consensus.get("ready"):
            return None
        return consensus.get("latest_confirmed_open_date")

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

    def trade_days_through(self, as_of: str) -> List[str]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT trade_date FROM research_trade_calendar WHERE trade_date<=? ORDER BY trade_date",
                (_iso_date(as_of),),
            )
            return [str(row[0]) for row in cur.fetchall()]
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

    def load_universe(self, as_of: str, *, cutoff_at: Optional[str] = None) -> pd.DataFrame:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT snapshot_id,snapshot_date FROM research_master_snapshot_runs
                   WHERE snapshot_date<=? AND quality_status='ok' AND published_at IS NOT NULL
                     AND (? IS NULL OR published_at<=?)
                   ORDER BY snapshot_date DESC,published_at DESC LIMIT 1""",
                (_iso_date(as_of), cutoff_at, cutoff_at),
            )
            row = cur.fetchone()
            snapshot_id = str(row[0]) if row and row[0] else None
            snapshot_date = str(row[1]) if row and row[1] else None
            if not snapshot_id:
                return pd.DataFrame()
            cur.execute(
                """SELECT symbol,name,exchange,market,industry,list_status,list_date,quality_status
                   FROM research_security_master_rows WHERE snapshot_id=?
                   AND (list_date='' OR list_date IS NULL OR list_date<=?)
                   AND (delist_date='' OR delist_date IS NULL OR delist_date>?)""",
                (snapshot_id, _iso_date(as_of), _iso_date(as_of)),
            )
            frame = self._frame(cur)
            frame.attrs["snapshot_id"] = snapshot_id
            frame.attrs["snapshot_date"] = snapshot_date
            return frame
        finally:
            conn.close()

    def load_lifecycle_universe(self, as_of: str) -> pd.DataFrame:
        """Reconstruct historical membership from the latest lifecycle observation.

        This method is intentionally separate from :meth:`load_universe`.  It may use a
        security lifecycle snapshot observed after ``as_of`` to recover listed/delisted
        membership, but current industry labels are not represented as historical PIT
        exposures.  Research callers must inspect the returned provenance attributes.
        """
        target = _iso_date(as_of)
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT snapshot_id,snapshot_date FROM research_master_snapshot_runs
                   WHERE quality_status='ok' AND published_at IS NOT NULL
                   ORDER BY snapshot_date DESC,published_at DESC LIMIT 1"""
            )
            row = cur.fetchone()
            snapshot_id = str(row[0]) if row and row[0] else None
            snapshot_date = str(row[1]) if row and row[1] else None
            if not snapshot_id:
                return pd.DataFrame()
            cur.execute(
                """SELECT symbol,name,exchange,market,industry,list_status,list_date,
                          delist_date,quality_status
                   FROM research_security_master_rows WHERE snapshot_id=?
                     AND (list_date='' OR list_date IS NULL OR list_date<=?)
                     AND (delist_date='' OR delist_date IS NULL OR delist_date>?)""",
                (snapshot_id, target, target),
            )
            frame = self._frame(cur)
            cur.execute(
                """SELECT DISTINCT list_status FROM research_security_master_rows
                   WHERE snapshot_id=?""",
                (snapshot_id,),
            )
            statuses = sorted(str(item[0] or "") for item in cur.fetchall() if item)
            frame.attrs.update({
                "snapshot_id": snapshot_id,
                "snapshot_date": snapshot_date,
                "membership_as_of": target,
                "membership_basis": "security_lifecycle_backfill",
                "observed_after_as_of": bool(snapshot_date and snapshot_date > target),
                "available_list_statuses": statuses,
                "lifecycle_complete": "L" in statuses and "D" in statuses,
                "historical_industry_complete": False,
            })
            return frame
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
            cur.execute(
                """SELECT snapshot_date FROM research_master_snapshot_runs
                   WHERE quality_status='ok' AND published_at IS NOT NULL
                   ORDER BY snapshot_date DESC,published_at DESC LIMIT 1"""
            )
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
                          is_paused,is_st,provider,quality_status,dataset_id
                   FROM research_daily_bars WHERE trade_date>=? AND trade_date<=? AND adjustment=?
                   ORDER BY symbol,trade_date""",
                (min(dates), as_of, adjustment),
            )
            return self._frame(cur)
        finally:
            conn.close()

    def latest_valuation_as_of(self, as_of: str) -> Optional[str]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT MAX(trade_date) FROM research_valuations
                   WHERE trade_date<=? AND quality_status NOT IN ('failed','unknown_unit')""",
                (_iso_date(as_of),),
            )
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None
        finally:
            conn.close()

    def load_valuation_evidence(self, as_of: str) -> list:
        """Resume same-day composite checkpoints, including not-yet-usable rows.

        Legacy zzshare data is already denominated in 亿元. Preserve its priority;
        do not allow an empty fallback response to wipe a previously valid field.
        """
        from data.valuation_contract import FIELDS, ALIASES, PRIORITY, number
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT symbol,provider,provider_effective_as_of,payload
                   FROM research_valuations WHERE trade_date=?
                   AND quality_status IN ('ok','partial')""", (_iso_date(as_of),))
            result = []
            for symbol, provider, effective, payload in cur.fetchall():
                if str(effective) != _iso_date(as_of):
                    continue
                payload = payload if isinstance(payload, dict) else json.loads(payload or '{}')
                if payload.get('valuation_contract') == 'composite-v1':
                    result.append(payload)
                elif provider in PRIORITY:
                    row = {'symbol': symbol, 'trade_date': str(effective),
                           'provider_effective_as_of': str(effective),
                           'market_cap_unit': 'CNY_100M', 'field_sources': {}}
                    for field in FIELDS:
                        value = number(payload.get(field, payload.get(ALIASES.get(field))))
                        if value is not None:
                            row[field] = value
                            row['field_sources'][field] = {'provider': provider, 'as_of': str(effective)}
                    result.append(row)
            return result
        finally:
            conn.close()

    def load_valuations(self, as_of: str, *, exact: bool = False) -> pd.DataFrame:
        conn = self.connect()
        try:
            cur = conn.cursor()
            valuation_date = _iso_date(as_of) if exact else self.latest_valuation_as_of(as_of)
            if not valuation_date:
                return pd.DataFrame()
            cur.execute(
                """SELECT symbol,trade_date,market_cap,circulating_market_cap,turnover_ratio,
                          pe_ttm,pe_lyr,pb,ps,pcf,dividend_yield,quality_status,requested_as_of,
                          provider_effective_as_of,dataset_id
                   FROM research_valuations WHERE trade_date=?
                   AND quality_status NOT IN ('failed','unknown_unit')""", (valuation_date,),
            )
            return self._frame(cur)
        finally:
            conn.close()

    def load_fund_flow_daily(self, as_of: str, *, exact: bool = False) -> pd.DataFrame:
        """Load only a completed, locally persisted main-force flow snapshot."""
        cutoff = _iso_date(as_of)
        conn = self.connect()
        try:
            cur = conn.cursor()
            if exact:
                flow_date = cutoff
            else:
                cur.execute(
                    """SELECT MAX(trade_date) FROM research_fund_flow_daily
                       WHERE trade_date<=? AND quality_status NOT IN ('failed','unknown')""",
                    (cutoff,),
                )
                row = cur.fetchone()
                flow_date = str(row[0]) if row and row[0] else ""
            if not flow_date:
                return pd.DataFrame()
            cur.execute(
                """SELECT symbol,trade_date,name,close,change_pct,main_net_inflow,
                          main_net_inflow_ratio,provider,quality_status,retrieved_at
                   FROM research_fund_flow_daily WHERE trade_date=?
                   AND quality_status NOT IN ('failed','unknown')""",
                (flow_date,),
            )
            return self._frame(cur)
        finally:
            conn.close()

    def load_fund_flow_panel(self, as_of: str, *, trading_days: int = 5) -> pd.DataFrame:
        """Load the most recent completed fund-flow dates up to ``as_of``.

        The current-date snapshot is still checked separately by the strategy
        engine.  This range loader exists only to measure persistence; it never
        turns an old snapshot into a current signal.
        """
        cutoff = _iso_date(as_of)
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT DISTINCT trade_date FROM research_fund_flow_daily
                   WHERE trade_date<=? AND quality_status NOT IN ('failed','unknown')
                   ORDER BY trade_date DESC LIMIT ?""",
                (cutoff, max(1, int(trading_days))),
            )
            dates = [str(row[0]) for row in cur.fetchall() if row and row[0]]
            if not dates:
                return pd.DataFrame()
            placeholders = ",".join("?" for _ in dates)
            cur.execute(
                f"""SELECT symbol,trade_date,name,close,change_pct,main_net_inflow,
                           main_net_inflow_ratio,provider,quality_status,retrieved_at
                    FROM research_fund_flow_daily
                    WHERE trade_date IN ({placeholders})
                      AND quality_status NOT IN ('failed','unknown')
                    ORDER BY symbol,trade_date""",
                tuple(dates),
            )
            return self._frame(cur)
        finally:
            conn.close()

    def pit_coverage(self, as_of: str) -> dict:
        """Describe the honest forward-PIT boundary without inferring historical visibility."""
        cutoff = _iso_date(as_of)
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT MIN(trade_date),MAX(CASE WHEN trade_date<=? THEN trade_date END)
                   FROM research_daily_bars""", (cutoff,)
            )
            market_start, market_end = cur.fetchone() or (None, None)
            cur.execute(
                "SELECT COUNT(DISTINCT trade_date) FROM research_daily_bars WHERE trade_date<=?",
                (cutoff,),
            )
            market_days = int((cur.fetchone() or [0])[0] or 0)
            cur.execute(
                """SELECT MIN(snapshot_date),MAX(CASE WHEN snapshot_date<=? THEN snapshot_date END)
                   FROM research_master_snapshot_runs
                   WHERE quality_status='ok' AND published_at IS NOT NULL""", (cutoff,)
            )
            universe_start, universe_end = cur.fetchone() or (None, None)
            cur.execute(
                """SELECT MIN(first_seen_as_of),
                          MAX(CASE WHEN first_seen_as_of<=? THEN first_seen_as_of END)
                   FROM research_financial_facts""", (cutoff,)
            )
            finance_start, finance_end = cur.fetchone() or (None, None)
            starts = [str(value) for value in (universe_start, finance_start) if value]
            coverage_start = max(starts) if len(starts) == 2 else None
            return {
                "market_history_start_date": str(market_start) if market_start else None,
                "market_history_end_date": str(market_end) if market_end else None,
                "market_history_days": market_days,
                "universe_pit_start_date": str(universe_start) if universe_start else None,
                "universe_pit_end_date": str(universe_end) if universe_end else None,
                "financial_pit_start_date": str(finance_start) if finance_start else None,
                "financial_pit_end_date": str(finance_end) if finance_end else None,
                "pit_coverage_start_date": coverage_start,
                "market_history_ready": market_days >= 70,
                "universe_pit_ready": bool(universe_start and universe_start <= cutoff),
                "financial_pit_ready": bool(finance_start and finance_start <= cutoff),
                "historical_pit_available": bool(coverage_start and cutoff >= coverage_start),
            }
        finally:
            conn.close()

    def load_financial_pit(self, table: str, as_of: str, *,
                           cutoff_at: Optional[str] = None) -> pd.DataFrame:
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
                         AND (? IS NULL OR first_seen_at<=?)
                   ) visible WHERE row_no=1""",
                (table, _iso_date(as_of), _iso_date(as_of), cutoff_at, cutoff_at),
            )
            frame = self._frame(cur)
            if not frame.empty:
                payloads = frame.pop("payload").map(lambda value: json.loads(value or "{}"))
                expanded = pd.json_normalize(payloads)
                frame = pd.concat([frame.reset_index(drop=True), expanded.reset_index(drop=True)], axis=1)
            return frame
        finally:
            conn.close()

    def load_financial_history(self, table: str, as_of: str, *,
                               cutoff_at: Optional[str] = None) -> pd.DataFrame:
        """Return the latest visible revision for every symbol/statement period."""
        cutoff = _iso_date(as_of)
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT symbol,first_seen_as_of AS as_of,stat_date,pub_date,revision_no,
                          provider,quality_status,payload FROM (
                       SELECT symbol,first_seen_as_of,stat_date,pub_date,revision_no,
                              provider,quality_status,payload,
                              ROW_NUMBER() OVER (
                                  PARTITION BY symbol,stat_date
                                  ORDER BY pub_date DESC,first_seen_as_of DESC,retrieved_at DESC
                              ) AS row_no
                       FROM research_financial_facts
                       WHERE table_name=? AND pub_date<=? AND first_seen_as_of<=?
                         AND (? IS NULL OR first_seen_at<=?)
                   ) visible WHERE row_no=1""",
                (str(table), cutoff, cutoff, cutoff_at, cutoff_at),
            )
            frame = self._frame(cur)
            if not frame.empty:
                payloads = frame.pop("payload").map(lambda value: json.loads(value or "{}"))
                expanded = pd.json_normalize(payloads)
                frame = pd.concat([frame.reset_index(drop=True), expanded.reset_index(drop=True)], axis=1)
            return frame
        finally:
            conn.close()

    def load_events(self, as_of: str, *, lookback_days: int = 120,
                    cutoff_at: Optional[str] = None) -> pd.DataFrame:
        start = (
            pd.Timestamp(as_of).to_pydatetime()
            - timedelta(days=int(lookback_days) * 2)
        ).date().isoformat()
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT symbol,event_type,event_date,effective_at,direction,confidence,
                          materiality,surprise,novelty,source_family,source_origin AS source,
                          event_cluster_id,entity_impact,official,title FROM (
                       SELECT symbol,event_type,event_date,effective_at,direction,confidence,
                              materiality,surprise,novelty,source_family,source_origin,
                              event_cluster_id,entity_impact,official,title,
                              confirmation_status,
                              ROW_NUMBER() OVER (
                                  PARTITION BY event_id ORDER BY retrieved_at DESC,revision_id DESC
                              ) AS row_no
                       FROM research_event_revisions
                       WHERE effective_at>=? AND effective_at<=?
                         AND (? IS NULL OR retrieved_at<=?)
                   ) visible WHERE row_no=1 AND confirmation_status='confirmed'""",
                (start, as_of, cutoff_at, cutoff_at),
            )
            return self._frame(cur)
        finally:
            conn.close()

    def event_dataset_id(self, as_of: str, *, cutoff_at: Optional[str] = None) -> str:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT revision_id,content_hash FROM (
                       SELECT revision_id,content_hash,event_id,confirmation_status,
                              ROW_NUMBER() OVER (
                                  PARTITION BY event_id ORDER BY retrieved_at DESC,revision_id DESC
                              ) AS row_no
                       FROM research_event_revisions
                       WHERE effective_at<=? AND (? IS NULL OR retrieved_at<=?)
                   ) visible WHERE row_no=1 AND confirmation_status='confirmed'
                   ORDER BY revision_id""",
                (_iso_date(as_of), cutoff_at, cutoff_at),
            )
            payload = "\n".join(f"{row[0]}:{row[1]}" for row in cur.fetchall())
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
        finally:
            conn.close()

    def load_selection_manifest(self, manifest_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT manifest_id,run_id,decision_context,universe_snapshot_id,
                          market_dataset_ids,valuation_dataset_ids,financial_revision_set_id,
                          event_dataset_id,policy_version,policy_hash,policy_payload,
                          code_revision,dependency_lock_hash,strategy_snapshot,
                          publication_generations,
                          schema_version,created_at
                   FROM selection_input_manifests WHERE manifest_id=?""",
                (str(manifest_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            keys = (
                "manifest_id", "run_id", "decision_context", "universe_snapshot_id",
                "market_dataset_ids", "valuation_dataset_ids", "financial_revision_set_id",
                "event_dataset_id", "policy_version", "policy_hash", "policy",
                "code_revision", "dependency_lock_hash", "strategy_snapshot",
                "publication_generations",
                "schema_version", "created_at",
            )
            value = dict(zip(keys, row))
            for key, fallback in (
                ("decision_context", {}), ("market_dataset_ids", []),
                ("valuation_dataset_ids", []), ("policy", {}),
                ("strategy_snapshot", {}),
                ("publication_generations", {}),
            ):
                if not isinstance(value[key], (dict, list)):
                    value[key] = json.loads(value[key] or _json(fallback))
            return value
        finally:
            conn.close()

    def load_universe_from_manifest(self, manifest_id: str) -> pd.DataFrame:
        manifest = self.load_selection_manifest(manifest_id)
        if not manifest:
            return pd.DataFrame()
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT symbol,name,exchange,market,industry,list_status,list_date,quality_status
                   FROM research_security_master_rows WHERE snapshot_id=?""",
                (manifest["universe_snapshot_id"],),
            )
            frame = self._frame(cur)
            frame.attrs["snapshot_id"] = manifest["universe_snapshot_id"]
            return frame
        finally:
            conn.close()

    @staticmethod
    def _observation_frame(rows: Sequence[tuple], *, kind: str) -> pd.DataFrame:
        values = []
        for row in rows:
            payload = row[-1] if isinstance(row[-1], dict) else json.loads(row[-1] or "{}")
            if kind == "market":
                values.append({
                    "symbol": row[0], "trade_date": row[1],
                    "open": payload.get("open"), "high": payload.get("high"),
                    "low": payload.get("low"), "close": payload.get("close"),
                    "volume": payload.get("volume", payload.get("vol")),
                    "amount": payload.get("turnover", payload.get("amount")),
                    "turnover_rate": payload.get("turnover_rate"),
                    "is_paused": int(bool(payload.get("is_paused", False))),
                    "is_st": int(bool(payload.get("is_st", False))),
                    "provider": row[2], "quality_status": row[3], "dataset_id": row[4],
                })
            else:
                values.append({
                    "symbol": row[0], "trade_date": row[1],
                    "market_cap": payload.get("market_cap"),
                    "circulating_market_cap": payload.get("circulating_market_cap"),
                    "turnover_ratio": payload.get("turnover_ratio"),
                    "pe_ttm": payload.get("pe_ratio", payload.get("pe_ttm")),
                    "pe_lyr": payload.get("pe_ratio_lyr", payload.get("pe_lyr")),
                    "pb": payload.get("pb_ratio", payload.get("pb")),
                    "ps": payload.get("ps_ratio", payload.get("ps")),
                    "pcf": payload.get("pcf_ratio", payload.get("pcf")),
                    "dividend_yield": next((payload.get(key) for key in (
                        "dividend_yield", "dividend_yield_ratio", "dv_ratio", "dividend_ratio",
                    ) if payload.get(key) is not None), None),
                    "quality_status": (payload.get("quality_status", "partial")
                                       if payload.get("valuation_contract") == "composite-v1" else row[3]),
                    "requested_as_of": row[2],
                    "provider_effective_as_of": row[1], "dataset_id": row[4],
                })
        return pd.DataFrame(values)

    def load_daily_panel_from_manifest(self, manifest_id: str, *, symbols=None) -> pd.DataFrame:
        manifest = self.load_selection_manifest(manifest_id)
        ids = list((manifest or {}).get("market_dataset_ids") or [])
        if not ids:
            return pd.DataFrame()
        placeholders = ",".join("?" for _ in ids)
        wanted = sorted({_symbol(v) for v in (symbols or []) if _symbol(v)})
        symbol_clause = (" AND o.symbol IN (" + ",".join("?" for _ in wanted) + ")") if wanted else ""
        context = (manifest or {}).get("decision_context") or {}
        cutoff = context.get("market_cutoff")
        boundary = " AND o.adjustment='qfq'"
        if cutoff:
            boundary += " AND o.trade_date" + ("<=?" if context.get("market_cutoff_inclusive") else "<?")
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT o.symbol,o.trade_date,o.provider,b.quality_status,o.dataset_id,o.payload
                   FROM research_market_observations o JOIN research_dataset_batches b
                     ON b.dataset_id=o.dataset_id
                   WHERE o.dataset_id IN (""" + placeholders + ")" + symbol_clause + boundary
                + " ORDER BY o.symbol,o.trade_date,o.retrieved_at,o.observation_id",
                (*ids, *wanted, *((cutoff,) if cutoff else ())),
            )
            frame = self._observation_frame(cur.fetchall(), kind="market")
            return frame if frame.empty else frame.drop_duplicates(["symbol", "trade_date"], keep="last")
        finally:
            conn.close()

    def load_valuations_from_manifest(self, manifest_id: str) -> pd.DataFrame:
        manifest = self.load_selection_manifest(manifest_id)
        ids = list((manifest or {}).get("valuation_dataset_ids") or [])
        if not ids:
            return pd.DataFrame()
        placeholders = ",".join("?" for _ in ids)
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT o.symbol,o.provider_effective_as_of,o.requested_as_of,
                          b.quality_status,o.dataset_id,o.payload
                   FROM research_valuation_observations o JOIN research_dataset_batches b
                     ON b.dataset_id=o.dataset_id
                   WHERE o.dataset_id IN (""" + placeholders + ") ORDER BY o.symbol",
                tuple(ids),
            )
            return self._observation_frame(cur.fetchall(), kind="valuation")
        finally:
            conn.close()

    def load_financial_facts_from_manifest(self, manifest_id: str) -> dict[str, pd.DataFrame]:
        manifest = self.load_selection_manifest(manifest_id)
        if not manifest:
            return {}
        context = manifest["decision_context"]
        selection_date = str(context["selection_date"])
        cutoff_at = str(context["financial_cutoff_at"])
        return {
            table: self.load_financial_history(table, selection_date, cutoff_at=cutoff_at)
            for table in ("indicator", "income", "balance", "cash_flow")
        }

    def load_events_from_manifest(self, manifest_id: str) -> pd.DataFrame:
        manifest = self.load_selection_manifest(manifest_id)
        if not manifest:
            return pd.DataFrame()
        context = manifest["decision_context"]
        frame = self.load_events(
            str(context["selection_date"]), cutoff_at=str(context["event_cutoff_at"])
        )
        actual = self.event_dataset_id(
            str(context["selection_date"]), cutoff_at=str(context["event_cutoff_at"])
        )
        if actual != manifest["event_dataset_id"]:
            raise ValueError("event revision set no longer matches the manifest")
        return frame

    def verify_selection_artifacts(self, run_id: str) -> dict:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT artifact_type,payload_hash,payload FROM selection_artifacts
                   WHERE run_id=? ORDER BY artifact_type""",
                (str(run_id),),
            )
            artifacts = []
            for artifact_type, expected, payload in cur.fetchall():
                normalized = payload if isinstance(payload, str) else _json(payload)
                actual = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                artifacts.append({
                    "artifact_type": artifact_type, "expected_hash": expected,
                    "actual_hash": actual, "exact_match": actual == expected,
                })
            return {
                "run_id": str(run_id), "exact_match": bool(artifacts)
                and all(item["exact_match"] for item in artifacts), "artifacts": artifacts,
            }
        finally:
            conn.close()

    def replay_selection(self, manifest_id: str, *, financial_overrides=None) -> dict:
        """Recompute a formal selection from immutable manifest inputs and compare artifacts."""
        manifest = self.load_selection_manifest(manifest_id)
        if not manifest:
            raise ValueError("selection manifest was not found")
        base = self

        class ManifestStore(ResearchStore):
            def __init__(self) -> None:
                pass

            def connect(self):
                return base.connect()

            def load_financial_history(self, table, as_of, *, cutoff_at=None):
                frame = base.load_financial_history(table, as_of, cutoff_at=cutoff_at)
                override = (financial_overrides or {}).get(table)
                if override and not frame.empty:
                    frame = frame.copy()
                    selected = frame["symbol"].astype(str).eq(override["symbol"])
                    stat = "stat_date" if "stat_date" in frame else "statDate"
                    selected &= frame[stat].astype(str).eq(override["stat_date"])
                    # Explicit hindsight counterfactual; preserve identity/timing
                    # columns of the original input, never publish this as PIT.
                    protected = {"symbol", "code", "ts_code", "stat_date", "statDate", "pub_date", "pubDate",
                                 "first_seen_at", "retrieved_at", "as_of", "revision_no", "provider"}
                    for key, value in override["values"].items():
                        if key not in protected and key in frame:
                            frame.loc[selected, key] = value
                return frame

            def load_universe(self, _as_of, *, cutoff_at=None):
                return base.load_universe_from_manifest(manifest_id)

            def load_daily_panel(self, _as_of, *, trading_days=500, adjustment="qfq"):
                frame = base.load_daily_panel_from_manifest(manifest_id)
                if frame.empty:
                    return frame
                dates = sorted(frame["trade_date"].astype(str).unique())[-int(trading_days):]
                return frame[frame["trade_date"].astype(str).isin(dates)].copy()

            def load_valuations(self, _as_of, *, exact=False):
                return base.load_valuations_from_manifest(manifest_id)

            def latest_valuation_as_of(self, _as_of):
                frame = base.load_valuations_from_manifest(manifest_id)
                return None if frame.empty else str(frame["trade_date"].max())

            def load_events(self, _as_of, *, lookback_days=120, cutoff_at=None):
                return base.load_events_from_manifest(manifest_id)

            def event_dataset_id(self, _as_of, *, cutoff_at=None):
                return str(manifest["event_dataset_id"])

            def pit_coverage(self, _as_of):
                value = base.pit_coverage(str(manifest["decision_context"]["universe_cutoff"]))
                value["historical_pit_available"] = True
                return value

            def calendar_consensus(self, _as_of, *, inclusive=False):
                frame = base.load_daily_panel_from_manifest(manifest_id)
                latest = None if frame.empty else str(frame["trade_date"].max())[:10]
                return {"ready": bool(latest), "latest_confirmed_open_date": latest}

        from analysis.local_stock_selector import LocalStockSelector, SelectionPolicy
        from analysis.selection_finalizer import finalize_local_selection

        context = manifest["decision_context"]
        selector = LocalStockSelector(
            store=ManifestStore(), policy=SelectionPolicy(**dict(manifest["policy"]))
        )
        replay = selector.run(
            str(context["selection_date"]),
            data_cutoff=str(context["market_cutoff"]),
            decision_at=str(context["decision_at"]),
            decision_mode=str(context["mode"]),
            wencai_reference=None,
            strategy_snapshot=manifest.get("strategy_snapshot") or None,
            persist=False,
        )
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT metadata FROM selection_runs WHERE run_id=?", (manifest["run_id"],))
            row = cur.fetchone()
            metadata = json.loads(row[0] or "{}") if row and not isinstance(row[0], dict) else (
                dict(row[0]) if row else {}
            )
            cur.execute(
                """SELECT artifact_type,payload_hash FROM selection_artifacts
                   WHERE run_id=? AND artifact_type IN ('formal_top15','formal_top5')""",
                (manifest["run_id"],),
            )
            expected = {str(item[0]): str(item[1]) for item in cur.fetchall()}
        finally:
            conn.close()
        identity = {
            "run_id": manifest["run_id"], "snapshot_id": metadata.get("snapshot_id"),
            "selection_date": context["selection_date"],
            "market_as_of": metadata.get("market_as_of"),
            "valuation_as_of": metadata.get("valuation_as_of"),
            "financial_as_of": metadata.get("financial_as_of"),
            "rule_version": metadata.get("rule_version"),
            "policy_hash": manifest["policy_hash"], "manifest_id": manifest_id,
        }
        formal15 = finalize_local_selection(
            [{**item, **identity, "rank": rank} for rank, item in enumerate(
                replay.get("candidates") or [], 1
            )],
            limit=15,
        )
        formal5 = finalize_local_selection(
            [{**item, **identity, "rank": rank} for rank, item in enumerate(
                replay.get("formal_top5") or [], 1
            )],
            limit=5,
        )
        actual = {
            "formal_top15": hashlib.sha256(_json(formal15).encode("utf-8")).hexdigest(),
            "formal_top5": hashlib.sha256(_json(formal5).encode("utf-8")).hexdigest(),
        }
        comparisons = {
            kind: {"expected_hash": expected.get(kind), "actual_hash": digest,
                   "exact_match": expected.get(kind) == digest}
            for kind, digest in actual.items()
        }
        return {
            "manifest_id": manifest_id, "run_id": manifest["run_id"],
            "replay_status": replay.get("status"),
            "reason": (replay.get("metadata") or {}).get("reason"),
            "frozen_market": self.manifest_market_coverage(manifest_id),
            "exact_match": replay.get("status") == "success"
            and all(item["exact_match"] for item in comparisons.values()),
            "artifacts": comparisons,
            **({"replay_class": "hindsight_correction_not_historical_pit", "formal_top15": formal15,
                "formal_top5": formal5, "published": False} if financial_overrides else {}),
        }

    def manifest_market_coverage(self, manifest_id: str) -> dict:
        """Diagnose missing immutable history without silently reading live bars."""
        manifest = self.load_selection_manifest(manifest_id)
        if not manifest:
            raise ValueError("selection manifest was not found")
        ids = list(manifest.get("market_dataset_ids") or [])
        counts = []
        if ids:
            context = manifest.get("decision_context") or {}
            cutoff = context.get("market_cutoff")
            boundary = ""
            if cutoff:
                boundary = " AND o.trade_date" + ("<=?" if context.get("market_cutoff_inclusive") else "<?")
            conn = self.connect()
            try:
                cur = conn.cursor()
                # Aggregate in PostgreSQL: diagnostics must not deserialize a
                # second multi-million-row OHLC panel on the NAS.
                cur.execute("SELECT o.symbol,COUNT(DISTINCT o.trade_date) "
                            "FROM research_market_observations o JOIN research_dataset_batches b "
                            "ON b.dataset_id=o.dataset_id WHERE o.dataset_id IN ("
                            + ",".join("?" for _ in ids) + ") AND o.adjustment='qfq'"
                            + boundary + " GROUP BY o.symbol", (*ids, *((cutoff,) if cutoff else ())))
                counts = [int(row[1]) for row in cur.fetchall()]
            finally:
                conn.close()
        policy = manifest.get("policy") or {}
        minimum = max(int(policy.get("min_history_days", 70)), int(policy.get("min_listing_trading_days", 0)))
        return {"rows": sum(counts), "symbols": len(counts),
                "max_history_days": max(counts, default=0),
                "required_history_days": minimum,
                "symbols_with_required_history": sum(count >= minimum for count in counts),
                "live_history_fallback": False}

    def save_selection(self, run: dict, primary: Sequence[dict], reference: Sequence[dict]) -> str:
        run_id = str(run.get("run_id") or uuid.uuid4().hex)
        metadata = dict(run.get("metadata", {}))
        input_manifest = dict(run.get("input_manifest") or {})
        formal_top15 = []
        formal_top5 = []
        if run.get("status") == "success":
            from analysis.selection_finalizer import finalize_local_selection
            identity = {
                "run_id": run_id,
                "snapshot_id": metadata.get("snapshot_id"),
                "selection_date": run.get("selection_date"),
                "market_as_of": metadata.get("market_as_of"),
                "valuation_as_of": metadata.get("valuation_as_of"),
                "financial_as_of": metadata.get("financial_as_of"),
                "rule_version": metadata.get("rule_version"),
                "policy_hash": metadata.get("policy_hash"),
                "manifest_id": metadata.get("manifest_id"),
            }
            formal_top15 = finalize_local_selection(
                [{**item, **identity, "rank": rank} for rank, item in enumerate(primary, 1)],
                limit=15,
            )
            formal_top5_source = list(run.get("formal_top5_candidates") or primary[:5])
            formal_top5 = finalize_local_selection(
                [{**item, **identity, "rank": rank} for rank, item in enumerate(
                    formal_top5_source, 1
                )],
                limit=5,
            )
            if not formal_top5:
                raise ValueError("successful selection requires a formal TOP5 artifact")
        conn = self.connect()
        try:
            created_at = datetime.now().astimezone().isoformat()
            cur = conn.cursor()
            metadata = {**metadata, "published_at": created_at if formal_top15 else None,
                        "selection_date": run["selection_date"],
                        "decision_at": created_at,
                        "data_cutoff_at": (metadata.get("decision_context") or {}).get("decision_at")}
            cur.execute(
                """SELECT run_id FROM selection_runs WHERE publication_status='published'
                   ORDER BY published_at DESC,created_at DESC LIMIT 1"""
            )
            previous = cur.fetchone()
            supersedes_run_id = str(previous[0]) if previous and previous[0] else None
            publication_status = "published" if formal_top15 else "unpublished"
            conn.execute(
                """INSERT INTO selection_runs
                   (run_id,selection_date,created_at,status,primary_source,universe_count,
                    eligible_count,final_count,coverage,reference_source,comparison,metadata,
                    publication_status,published_at,supersedes_run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, run["selection_date"], created_at,
                 run.get("status", "success"),
                 "local_fusion" if metadata.get("primary_pipeline") == "local_fusion" else "local_pit",
                 int(run.get("universe_count", 0)),
                 int(run.get("eligible_count", 0)), len(primary), float(run.get("coverage", 0)),
                 "wencai" if reference else None, _json(run.get("comparison", {})),
                 _json(metadata), publication_status,
                 created_at if formal_top15 else None,
                 supersedes_run_id if formal_top15 else None),
            )
            rows = []
            for kind, candidates in (("diagnostic", primary), ("wencai_reference", reference)):
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
            if formal_top15:
                manifest_id = str(input_manifest.get("manifest_id") or "")
                if not manifest_id or manifest_id != metadata.get("manifest_id"):
                    raise ValueError("formal selection requires a matching input manifest")
                conn.execute(
                    """INSERT INTO selection_input_manifests
                       (manifest_id,run_id,decision_context,universe_snapshot_id,
                        market_dataset_ids,valuation_dataset_ids,financial_revision_set_id,
                        event_dataset_id,policy_version,policy_hash,policy_payload,
                        code_revision,dependency_lock_hash,strategy_snapshot,
                        publication_generations,schema_version,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (manifest_id, run_id, _json(input_manifest.get("decision_context", {})),
                     input_manifest.get("universe_snapshot_id", ""),
                     _json(input_manifest.get("market_dataset_ids", [])),
                     _json(input_manifest.get("valuation_dataset_ids", [])),
                     input_manifest.get("financial_revision_set_id", ""),
                     input_manifest.get("event_dataset_id", ""),
                     input_manifest.get("policy_version", ""),
                     input_manifest.get("policy_hash", ""),
                     _json(input_manifest.get("policy", {})),
                     input_manifest.get("code_revision", "unknown"),
                     input_manifest.get("dependency_lock_hash", "unknown"),
                     _json(input_manifest.get("strategy_snapshot", {})),
                     _json(input_manifest.get("publication_generations", {})), SCHEMA_VERSION,
                     datetime.now().astimezone().isoformat()),
                )
                self._insert_selection_artifact(conn, run_id, "formal_top15", formal_top15, metadata)
                self._insert_selection_artifact(conn, run_id, "formal_top5", formal_top5, metadata)
                from application.decision_capsule import build_capsule
                from analysis.account_action_plan import model_portfolio
                cur.execute("SELECT MIN(trade_date) FROM research_trade_calendar WHERE trade_date>?",
                            (created_at[:10],))
                next_day = cur.fetchone()
                capsule = build_capsule(
                    run_id=run_id, metadata=metadata, top15=formal_top15, top5=formal_top5,
                    published_at=created_at, next_open_date=str(next_day[0]) if next_day and next_day[0] else None)
                self._insert_selection_artifact(conn, run_id, "decision_capsule", capsule, metadata)
                self._insert_selection_artifact(conn, run_id, "model_portfolio", model_portfolio(capsule), metadata)
                from application.research_artifacts import build_selection_research_artifact

                research_artifact = build_selection_research_artifact(
                    run_id=run_id, selection_date=run["selection_date"],
                    candidates=formal_top5, metadata=metadata, input_manifest=input_manifest,
                )
                conn.execute(
                    """INSERT INTO research_artifacts
                       (artifact_id,subject,artifact_kind,run_id,formal,schema_version,
                        payload_hash,payload,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,artifact_kind) DO NOTHING""",
                    (research_artifact["artifact_id"], research_artifact["subject"],
                     research_artifact["artifact_kind"], run_id, 1,
                     research_artifact["schema_version"], research_artifact["payload_hash"],
                     _json(research_artifact), research_artifact["created_at"]),
                )
            conn.commit()
            return run_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _insert_selection_artifact(conn, run_id: str, artifact_type: str,
                                   payload: object, metadata: dict) -> str:
        payload_text = _json(payload)
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        artifact_id = hashlib.sha256(
            f"{run_id}:{artifact_type}:{payload_hash}".encode("utf-8")
        ).hexdigest()
        conn.execute(
            """INSERT INTO selection_artifacts
               (artifact_id,run_id,artifact_type,parent_snapshot_id,rule_version,
                policy_hash,payload_hash,payload,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (artifact_id, str(run_id), str(artifact_type), metadata.get("snapshot_id"),
             metadata.get("rule_version", ""), metadata.get("policy_hash", ""),
             payload_hash, payload_text, datetime.now().astimezone().isoformat()),
        )
        return artifact_id

    def save_selection_artifact(self, run_id: str, artifact_type: str,
                                payload: object) -> str:
        """Append one attachment; an existing run/type pair is never overwritten."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT metadata FROM selection_runs WHERE run_id=?", (str(run_id),))
            row = cur.fetchone()
            if not row:
                raise ValueError("selection run not found")
            metadata = json.loads(row[0] or "{}")
            artifact_id = self._insert_selection_artifact(
                conn, str(run_id), str(artifact_type), payload, metadata
            )
            conn.commit()
            return artifact_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_selection_strategy_records(self, run_id: str, nominations: Sequence[dict],
                                        *, policy: dict, policy_hash: str,
                                        selection_date: str,
                                        input_snapshot_id: str = "",
                                        persist_policy: bool = True) -> int:
        """Persist multi-strategy nominations as normalized immutable facts."""
        now = datetime.now().astimezone().isoformat()
        grouped: Dict[tuple, List[dict]] = {}
        for raw in nominations or ():
            item = dict(raw or {})
            key = (
                str(item.get("strategy_id") or ""),
                str(item.get("strategy_version") or ""),
                str(item.get("lane") or ""),
            )
            if key[0] and _symbol(item.get("symbol")):
                grouped.setdefault(key, []).append(item)
        conn = self.connect()
        inserted = 0
        try:
            cur = conn.cursor()
            if persist_policy:
                if self._is_postgres:
                    cur.execute("SELECT pg_advisory_xact_lock(1936482715)")
                else:
                    cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "SELECT 1 FROM strategy_policy_versions WHERE policy_version=? AND policy_hash=?",
                    (str(policy.get("version") or "local-fusion-v2"), str(policy_hash)),
                )
                if not cur.fetchone():
                    # Recording a selection is not authority to activate a policy.
                    # Only bootstrap an empty registry; later activation uses CAS.
                    cur.execute("SELECT 1 FROM strategy_policy_versions WHERE state IN ('active','scheduled') LIMIT 1")
                    policy_state = "observed" if cur.fetchone() else "active"
                    cur.execute(
                        """INSERT INTO strategy_policy_versions
                           (policy_version,policy_hash,state,effective_from,payload,created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (str(policy.get("version") or "local-fusion-v2"), str(policy_hash),
                         policy_state, str(selection_date), _json(policy), now),
                    )
            for (strategy_id, strategy_version, lane), rows in grouped.items():
                strategy_run_id = hashlib.sha256(
                    f"{run_id}:{strategy_id}:{strategy_version}".encode("utf-8")
                ).hexdigest()
                cur.execute(
                    """INSERT INTO selection_strategy_runs
                       (strategy_run_id,selection_run_id,strategy_id,strategy_version,lane,
                        status,data_as_of,input_snapshot_id,policy_version,metadata,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (strategy_run_id, str(run_id), strategy_id, strategy_version, lane,
                     "success", str(selection_date), str(input_snapshot_id),
                     str(policy.get("version") or "local-fusion-v2"),
                     _json({"nomination_count": len(rows)}), now),
                )
                for item in rows:
                    symbol = _symbol(item.get("symbol"))
                    nomination_id = hashlib.sha256(
                        f"{strategy_run_id}:{symbol}".encode("utf-8")
                    ).hexdigest()
                    cur.execute(
                        """INSERT INTO selection_candidate_nominations
                           (nomination_id,selection_run_id,strategy_run_id,symbol,lane,
                            strategy_id,strategy_version,lane_rank,lane_score_raw,
                            priority_weight,eligibility,evidence,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (nomination_id, str(run_id), strategy_run_id, symbol, lane,
                         strategy_id, strategy_version, int(item.get("lane_rank") or 0),
                         _plain(item.get("lane_score_raw")),
                         float(item.get("priority_weight") or 0), "eligible",
                         _json(item.get("evidence") or {}), now),
                    )
                    inserted += 1
            conn.commit()
            return inserted
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_selection_candidate_outcomes(
            self, *, horizons: Sequence[int] = (1, 3, 5, 10, 20),
            limit: int = 10000) -> dict:
        """Mature incomplete nomination outcomes with next-open entry semantics.

        Fully matured nominations are excluded before applying ``limit``. This keeps
        the 20-day horizon reachable as producer/reference strategies add more rows.
        """
        wanted = sorted({max(1, int(value)) for value in horizons})
        conn = self.connect()
        updated = 0
        pending = 0
        try:
            cur = conn.cursor()
            horizon_placeholders = ",".join("?" for _ in wanted)
            cur.execute(
                f"""SELECT n.nomination_id,n.symbol,r.data_as_of
                    FROM selection_candidate_nominations n
                    JOIN selection_strategy_runs r ON r.strategy_run_id=n.strategy_run_id
                    WHERE EXISTS (
                              SELECT 1 FROM research_daily_bars b
                              WHERE b.symbol=n.symbol AND b.trade_date>r.data_as_of
                                AND b.adjustment='qfq'
                                AND b.quality_status NOT IN ('unknown_unit','failed')
                          )
                      AND (SELECT COUNT(DISTINCT o.horizon_days)
                           FROM selection_candidate_outcomes o
                           WHERE o.nomination_id=n.nomination_id
                             AND o.outcome_status='matured'
                             AND o.horizon_days IN ({horizon_placeholders})) < ?
                    ORDER BY n.created_at ASC LIMIT ?""",
                (*wanted, len(wanted), max(1, int(limit))),
            )
            nominations = list(cur.fetchall())
            now = datetime.now().astimezone().isoformat()
            for nomination_id, symbol, signal_date in nominations:
                cur.execute(
                    """SELECT trade_date,open,close,low FROM research_daily_bars
                       WHERE symbol=? AND trade_date>? AND adjustment='qfq'
                         AND quality_status NOT IN ('unknown_unit','failed')
                       ORDER BY trade_date""",
                    (str(symbol), str(signal_date)),
                )
                bars = list(cur.fetchall())
                for horizon in wanted:
                    if len(bars) < horizon:
                        pending += 1
                        continue
                    window = bars[:horizon]
                    entry = _plain(window[0][1])
                    exit_price = _plain(window[-1][2])
                    if not entry or not exit_price or float(entry) <= 0:
                        pending += 1
                        continue
                    ret = (float(exit_price) / float(entry) - 1.0) * 100.0
                    lows = [float(row[3]) for row in window if row[3] is not None]
                    from analysis.decision_evaluation import price_metrics
                    try:
                        metrics = price_metrics(entry, [row[2] for row in window], lows)
                    except (ValueError, TypeError):
                        pending += 1
                        continue
                    max_dd = metrics["close_max_drawdown_pct"]
                    values = (
                        str(window[0][0]), float(entry), str(window[-1][0]), float(exit_price),
                        round(ret, 6), None, round(max_dd, 6) if max_dd is not None else None,
                        "matured", now, str(nomination_id), horizon,
                    )
                    cur.execute(
                        "SELECT 1 FROM selection_candidate_outcomes WHERE nomination_id=? AND horizon_days=?",
                        (str(nomination_id), horizon),
                    )
                    if cur.fetchone():
                        cur.execute(
                            """UPDATE selection_candidate_outcomes SET
                               entry_date=?,entry_price=?,exit_date=?,exit_price=?,return_pct=?,
                               benchmark_return_pct=?,max_drawdown_pct=?,outcome_status=?,evaluated_at=?
                               WHERE nomination_id=? AND horizon_days=?""", values,
                        )
                    else:
                        cur.execute(
                            """INSERT INTO selection_candidate_outcomes
                               (entry_date,entry_price,exit_date,exit_price,return_pct,
                                benchmark_return_pct,max_drawdown_pct,outcome_status,evaluated_at,
                                nomination_id,horizon_days) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            values,
                        )
                    updated += 1
                    cur.execute(
                        "UPDATE selection_candidate_outcomes SET mae_pct=?,metric_version=? "
                        "WHERE nomination_id=? AND horizon_days=?",
                        (metrics["mae_pct"], metrics["metric_version"], str(nomination_id), horizon))
            conn.commit()
            return {"updated": updated, "pending": pending, "horizons": wanted,
                    "nomination_count": len(nominations)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def resolve_trade_origin(self, symbol: str, *, selection_run_id: str = "",
                             nomination_id: str = "", strategy_id: str = "") -> Optional[dict]:
        """Validate optional trade attribution against immutable selection facts."""
        wanted_symbol = _symbol(symbol)
        wanted_run = str(selection_run_id or "").strip()
        wanted_nomination = str(nomination_id or "").strip()
        wanted_strategy = str(strategy_id or "").strip()
        if not wanted_symbol or not (wanted_run or wanted_nomination):
            return None
        conn = self.connect()
        try:
            cur = conn.cursor()
            if wanted_nomination:
                cur.execute(
                    """SELECT nomination_id,selection_run_id,strategy_id,symbol,lane
                       FROM selection_candidate_nominations
                       WHERE nomination_id=? AND symbol=?""",
                    (wanted_nomination, wanted_symbol),
                )
                row = cur.fetchone()
                if not row:
                    return None
                resolved = {
                    "nomination_id": str(row[0]),
                    "selection_run_id": str(row[1]),
                    "strategy_id": str(row[2]),
                    "symbol": str(row[3]),
                    "lane": str(row[4]),
                }
                if wanted_run and resolved["selection_run_id"] != wanted_run:
                    return None
                if wanted_strategy and resolved["strategy_id"] != wanted_strategy:
                    return None
                return resolved
            if wanted_run and wanted_strategy:
                cur.execute(
                    """SELECT nomination_id,selection_run_id,strategy_id,symbol,lane
                       FROM selection_candidate_nominations
                       WHERE selection_run_id=? AND strategy_id=? AND symbol=?
                       ORDER BY lane_rank ASC LIMIT 1""",
                    (wanted_run, wanted_strategy, wanted_symbol),
                )
                row = cur.fetchone()
                return ({
                    "nomination_id": str(row[0]), "selection_run_id": str(row[1]),
                    "strategy_id": str(row[2]), "symbol": str(row[3]),
                    "lane": str(row[4]),
                } if row else None)
            cur.execute(
                """SELECT 1 FROM selection_candidates
                   WHERE run_id=? AND symbol=? LIMIT 1""",
                (wanted_run, wanted_symbol),
            )
            return ({
                "nomination_id": None, "selection_run_id": wanted_run,
                "strategy_id": None, "symbol": wanted_symbol, "lane": None,
            } if cur.fetchone() else None)
        finally:
            conn.close()

    def selection_strategy_evidence(self, *, horizon_days: int = 5,
                                    lookback_days: int = 180) -> dict:
        """Return deterministic evidence consumed by the weekly policy committee."""
        since = (datetime.now().astimezone() - timedelta(days=max(1, lookback_days))).isoformat()
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT n.strategy_id,n.lane,o.return_pct,o.max_drawdown_pct,
                          n.created_at,n.symbol,n.strategy_version,o.metric_version,o.mae_pct,
                          o.entry_date,o.exit_date
                   FROM selection_candidate_nominations n
                   JOIN selection_candidate_outcomes o ON o.nomination_id=n.nomination_id
                   WHERE o.horizon_days=? AND o.outcome_status='matured' AND n.created_at>=?""",
                (max(1, int(horizon_days)), since),
            )
            buckets: Dict[str, dict] = {}
            for strategy_id, lane, ret, drawdown, created_at, symbol, strategy_version, metric_version, mae, entry_date, exit_date in cur.fetchall():
                bucket = buckets.setdefault((str(strategy_id), str(strategy_version), str(metric_version)), {
                    "strategy_id": str(strategy_id), "lane": str(lane), "returns": [],
                    "strategy_version": str(strategy_version), "metric_version": str(metric_version),
                    "drawdowns": [], "maes": [], "dates": set(), "symbols": set(), "intervals": set(),
                })
                if ret is not None:
                    bucket["returns"].append(float(ret))
                if drawdown is not None and metric_version == "signal-price-v2":
                    bucket["drawdowns"].append(float(drawdown))
                elif drawdown is not None:
                    bucket["maes"].append(min(0., float(drawdown)))
                if mae is not None:
                    bucket["maes"].append(float(mae))
                bucket["dates"].add(str(entry_date))
                bucket["intervals"].add((str(entry_date), str(exit_date)))
                bucket["symbols"].add(str(symbol))
            strategies = []
            for bucket in buckets.values():
                returns = bucket.pop("returns")
                drawdowns = bucket.pop("drawdowns")
                dates = bucket.pop("dates")
                symbols = bucket.pop("symbols")
                maes = bucket.pop("maes")
                intervals = sorted(bucket.pop("intervals"))
                disjoint, last_end = 0, ""
                for start, end in intervals:
                    if start > last_end:
                        disjoint, last_end = disjoint + 1, end
                strategies.append({
                    **bucket, "sample_size": len(returns), "independent_dates": len(dates),
                    "nonoverlapping_price_intervals": disjoint,
                    "effective_samples": 0,
                    "promotion_blocker": "price_labels_are_not_independent_executable_net_evidence",
                    "symbol_count": len(symbols),
                    "win_rate_pct": round(sum(value > 0 for value in returns) / len(returns) * 100, 2)
                    if returns else None,
                    "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
                    "worst_drawdown_pct": round(min(drawdowns), 4) if drawdowns else None,
                    "worst_mae_pct": round(min(maes), 4) if maes else None,
                })
            strategies.sort(key=lambda item: (item["lane"], item["strategy_id"]))
            cur.execute(
                """SELECT n.selection_run_id,n.symbol,n.lane,n.lane_rank,o.return_pct
                   FROM selection_candidate_nominations n
                   JOIN selection_candidate_outcomes o ON o.nomination_id=n.nomination_id
                   WHERE o.horizon_days=? AND o.outcome_status='matured' AND n.created_at>=?""",
                (max(1, int(horizon_days)), since),
            )
            by_run: Dict[str, Dict[str, dict]] = {}
            for run_id, symbol, lane, lane_rank, ret in cur.fetchall():
                by_run.setdefault(str(run_id), {}).setdefault(str(symbol), {
                    "return_pct": float(ret), "core_rank": None,
                })
                if str(lane) == "core":
                    current_rank = by_run[str(run_id)][str(symbol)].get("core_rank")
                    rank_value = int(lane_rank or 9999)
                    by_run[str(run_id)][str(symbol)]["core_rank"] = min(
                        current_rank if current_rank is not None else rank_value, rank_value
                    )
            cur.execute(
                """SELECT run_id,symbol FROM selection_candidates
                   WHERE candidate_kind='diagnostic'"""
            )
            selected_by_run: Dict[str, set] = {}
            for run_id, symbol in cur.fetchall():
                selected_by_run.setdefault(str(run_id), set()).add(str(symbol))
            comparisons = []
            for run_id, symbol_rows in by_run.items():
                selected = selected_by_run.get(run_id) or set()
                fusion_returns = [row["return_pct"] for symbol, row in symbol_rows.items()
                                  if symbol in selected]
                pit_returns = [row["return_pct"] for row in symbol_rows.values()
                               if row.get("core_rank") is not None and row["core_rank"] <= 15]
                if fusion_returns and pit_returns:
                    fusion_avg = sum(fusion_returns) / len(fusion_returns)
                    pit_avg = sum(pit_returns) / len(pit_returns)
                    comparisons.append({
                        "selection_run_id": run_id,
                        "fusion_return_pct": round(fusion_avg, 4),
                        "pit_only_return_pct": round(pit_avg, 4),
                        "satellite_marginal_pct": round(fusion_avg - pit_avg, 4),
                        "fusion_sample_size": len(fusion_returns),
                        "pit_sample_size": len(pit_returns),
                    })
            marginal_values = [item["satellite_marginal_pct"] for item in comparisons]
            portfolio_comparison = {
                "matured_runs": len(comparisons),
                "avg_satellite_marginal_pct": round(
                    sum(marginal_values) / len(marginal_values), 4
                ) if marginal_values else None,
                "runs": comparisons[-30:],
            }
            payload = {"horizon_days": horizon_days, "lookback_days": lookback_days,
                       "strategies": strategies,
                       "portfolio_comparison": portfolio_comparison}
            encoded = _json(payload)
            payload["evidence_snapshot_id"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            return payload
        finally:
            conn.close()

    def load_active_strategy_policy(self) -> Optional[dict]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT policy_version,policy_hash,payload,effective_from,created_at
                   FROM strategy_policy_versions WHERE state IN ('active','scheduled')
                     AND effective_from<=?
                   ORDER BY effective_from DESC,created_at DESC LIMIT 1""",
                (datetime.now().astimezone().isoformat(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            payload = row[2] if isinstance(row[2], dict) else json.loads(row[2] or "{}")
            return {"policy_version": str(row[0]), "policy_hash": str(row[1]),
                    "payload": payload, "effective_from": str(row[3]),
                    "created_at": str(row[4])}
        finally:
            conn.close()

    def save_strategy_policy_proposal(self, proposal: dict, *, validation_status: str,
                                      validation_reason: str = "",
                                      applied_policy: Optional[dict] = None) -> str:
        now = datetime.now().astimezone().isoformat()
        proposal_id = str(proposal.get("proposal_id") or hashlib.sha256(
            _json(proposal).encode("utf-8")
        ).hexdigest())
        applied_hash = None
        conn = self.connect()
        try:
            cur = conn.cursor()
            # One transaction owns comparison, proposal idempotency and publication.
            if self._is_postgres:
                cur.execute("SELECT pg_advisory_xact_lock(1936482715)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT proposal FROM strategy_adjustment_proposals WHERE proposal_id=?", (proposal_id,))
            existing = cur.fetchone()
            if existing:
                if (existing[0] if isinstance(existing[0], dict) else json.loads(existing[0])) != proposal:
                    raise ValueError("proposal_id_reused_with_different_payload")
                conn.rollback()
                return proposal_id
            if applied_policy:
                cur.execute("SELECT policy_hash FROM strategy_policy_versions "
                            "WHERE state IN ('active','scheduled') ORDER BY created_at DESC LIMIT 1")
                active_row = cur.fetchone()
                if not active_row or str(active_row[0]) != str(proposal.get("base_policy_hash")):
                    raise ValueError("policy_compare_and_swap_conflict")
                payload = dict(applied_policy)
                encoded = _json(payload)
                # Same canonical contract as FusionPolicy.policy_hash. Unsorted
                # storage JSON must not create a second identity for one policy.
                from application.results import payload_hash
                applied_hash = payload_hash(payload)
                version = str(payload.get("version") or f"local-fusion-{now[:10]}")
                cur.execute(
                    """INSERT INTO strategy_policy_versions
                       (policy_version,policy_hash,state,effective_from,payload,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (version, applied_hash, "scheduled", str(proposal.get("effective_from") or now),
                     encoded, now),
                )
            cur.execute(
                """INSERT INTO strategy_adjustment_proposals
                   (proposal_id,base_policy_hash,evidence_snapshot_id,proposal,
                    validation_status,validation_reason,applied_policy_hash,created_at,applied_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (proposal_id, str(proposal.get("base_policy_hash") or ""),
                 str(proposal.get("evidence_snapshot_id") or ""), _json(proposal),
                 str(validation_status), str(validation_reason or ""), applied_hash,
                 now, now if applied_hash else None),
            )
            conn.commit()
            return proposal_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def attach_selection_reference(self, run_id: str, reference: Sequence[dict],
                                   comparison: dict) -> None:
        """Attach best-effort external discovery after the formal local run is persisted."""
        conn = self.connect()
        try:
            conn.execute(
                """UPDATE selection_runs SET reference_source=?,comparison=?
                   WHERE run_id=?""",
                ("wencai" if reference else None, _json(comparison), str(run_id)),
            )
            rows = []
            for rank, item in enumerate(reference, 1):
                symbol = _symbol(item.get("symbol"))
                if not symbol:
                    continue
                rows.append((
                    str(run_id), symbol, "wencai_reference", rank,
                    None, None, None, None, None, None, None, None, None,
                    None, None, "[]", _json(item.get("source_labels", [])), _json(item),
                ))
            if rows:
                conn.cursor().executemany(
                    """INSERT INTO selection_candidates
                       (run_id,symbol,candidate_kind,rank_pos,total_score,fundamental_score,
                        technical_60_score,industry_score,quality_score,correction_120,
                        correction_250,event_correction,data_coverage,state,industry,reasons,
                        source_labels,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,symbol,candidate_kind) DO UPDATE SET
                        rank_pos=excluded.rank_pos,source_labels=excluded.source_labels,
                        payload=excluded.payload""",
                    rows,
                )
            cur = conn.cursor()
            cur.execute("SELECT metadata FROM selection_runs WHERE run_id=?", (str(run_id),))
            run_row = cur.fetchone()
            cur.execute(
                "SELECT 1 FROM selection_artifacts WHERE run_id=? AND artifact_type='external_reference'",
                (str(run_id),),
            )
            if run_row and not cur.fetchone():
                self._insert_selection_artifact(
                    conn, str(run_id), "external_reference",
                    {"reference": list(reference), "comparison": comparison},
                    json.loads(run_row[0] or "{}"),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _latest_selection_where(self, where_sql: str = "", params: Sequence[object] = ()) -> Optional[dict]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT run_id,selection_date,status,comparison,metadata "
                f"FROM selection_runs {where_sql} ORDER BY created_at DESC LIMIT 1",
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                return None
            run_id, selection_date, status, comparison, metadata = row
            cur.execute(
                """SELECT artifact_type,artifact_id,payload_hash,payload,created_at
                   FROM selection_artifacts WHERE run_id=?
                   ORDER BY created_at,artifact_type""", (run_id,),
            )
            artifacts = {}
            for artifact_type, artifact_id, payload_hash, payload, created_at in cur.fetchall():
                artifacts[str(artifact_type)] = {
                    "artifact_id": str(artifact_id), "payload_hash": str(payload_hash),
                    "payload": json.loads(payload or "[]"), "created_at": str(created_at),
                }
            formal = artifacts.get("formal_top15", {}).get("payload", [])
            return {
                "run_id": run_id, "selection_date": selection_date, "status": status,
                "comparison": json.loads(comparison or "{}"), "metadata": json.loads(metadata or "{}"),
                "candidates": formal, "artifacts": artifacts,
            }
        finally:
            conn.close()

    def latest_selection_attempt(self) -> Optional[dict]:
        """Return the newest attempt, including failed or incomplete diagnostic runs."""

        return self._latest_selection_where()

    def latest_formal_selection(self) -> Optional[dict]:
        """Return only a successful, manifest-bound and fully published formal result."""

        return self._latest_selection_where(
            """WHERE status='success' AND publication_status='published'
               AND EXISTS (
                   SELECT 1 FROM selection_input_manifests m
                   WHERE m.run_id=selection_runs.run_id
               )
               AND EXISTS (
                   SELECT 1 FROM selection_artifacts a
                   WHERE a.run_id=selection_runs.run_id AND a.artifact_type='formal_top15'
               )
               AND EXISTS (
                   SELECT 1 FROM selection_artifacts a
                   WHERE a.run_id=selection_runs.run_id AND a.artifact_type='formal_top5'
               )"""
        )

    def latest_selection(self) -> Optional[dict]:
        """Backward-compatible diagnostic alias; formal readers must call the explicit method."""

        return self.latest_selection_attempt()
