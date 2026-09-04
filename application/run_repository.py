"""Persistent preview Run, idempotency and outbox repository."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from application.results import canonical_json, clean_json, now_iso, payload_hash


class IdempotencyConflict(ValueError):
    """The same actor/capability/key was used with a different canonical request."""


class RunQuotaExceeded(ValueError):
    """The actor already owns the configured number of active Runs."""


@dataclass(frozen=True)
class CreateRunResult:
    run: dict[str, Any]
    created: bool


class RunRepository:
    def __init__(
        self,
        connect_fn: Callable[..., Any] | None = None,
        *,
        is_postgres: bool | None = None,
        ensure_schema: bool = True,
    ) -> None:
        if connect_fn is None:
            from db_compat import connect as db_connect

            connect_fn = db_connect
        self._connect_fn = connect_fn
        self._is_postgres = (connect_fn.__module__ == "db_compat") if is_postgres is None else is_postgres
        if ensure_schema:
            self.ensure_schema()

    def connect(self):
        return self._connect_fn("foliant_runs")

    def ensure_schema(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS foliant_runs (
                run_id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                run_kind TEXT NOT NULL,
                mode TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                request_payload TEXT NOT NULL,
                request_id TEXT,
                status TEXT NOT NULL,
                cancellable INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                resource_uri TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                result_payload TEXT,
                provenance TEXT NOT NULL DEFAULT '{}',
                warnings TEXT NOT NULL DEFAULT '[]',
                error_code TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                failed_at TEXT,
                worker_id TEXT,
                fencing_token TEXT,
                lease_until TEXT,
                heartbeat_at TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 2,
                next_attempt_at TEXT,
                timeout_seconds INTEGER NOT NULL DEFAULT 1800,
                updated_at TEXT NOT NULL,
                UNIQUE(actor_id, capability, idempotency_key)
            )""",
            """CREATE TABLE IF NOT EXISTS foliant_run_attempts (
                run_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                worker_id TEXT NOT NULL,
                fencing_token TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                heartbeat_at TEXT,
                completed_at TEXT,
                error_code TEXT,
                PRIMARY KEY(run_id, attempt),
                UNIQUE(fencing_token)
            )""",
            """CREATE TABLE IF NOT EXISTS foliant_run_progress (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                phase TEXT NOT NULL,
                current_value INTEGER,
                total_value INTEGER,
                message_code TEXT,
                created_at TEXT NOT NULL
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
            """CREATE TABLE IF NOT EXISTS foliant_domain_outbox (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                resource_uri TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_by TEXT,
                lease_until TEXT,
                last_error TEXT,
                dead_letter_at TEXT,
                UNIQUE(run_id, event_type)
            )""",
            """CREATE TABLE IF NOT EXISTS foliant_write_idempotency (
                actor_id TEXT NOT NULL,
                action TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(actor_id, action, idempotency_key)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_foliant_runs_status ON foliant_runs(status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_foliant_runs_actor ON foliant_runs(actor_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_foliant_run_progress_run ON foliant_run_progress(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_foliant_outbox_pending ON foliant_domain_outbox(published_at, created_at)",
        ]
        from data.reliability_store import SCHEMA as reliability_schema
        statements.extend(reliability_schema)
        conn = self.connect()
        try:
            cur = conn.cursor()
            for statement in statements:
                cur.execute(statement)
            columns = (
                ("worker_id", "TEXT"), ("fencing_token", "TEXT"), ("lease_until", "TEXT"),
                ("heartbeat_at", "TEXT"), ("attempt", "INTEGER NOT NULL DEFAULT 0"),
                ("max_attempts", "INTEGER NOT NULL DEFAULT 2"),
                ("next_attempt_at", "TEXT"),
                ("timeout_seconds", "INTEGER NOT NULL DEFAULT 1800"),
            )
            if self._is_postgres:
                for name, declaration in columns:
                    cur.execute(
                        f"ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS {name} {declaration}"
                    )
            else:
                cur.execute("PRAGMA table_info(foliant_runs)")
                existing_columns = {str(row[1]) for row in cur.fetchall()}
                for name, declaration in columns:
                    if name not in existing_columns:
                        cur.execute(f"ALTER TABLE foliant_runs ADD COLUMN {name} {declaration}")
            outbox_columns = (
                ("claimed_by", "TEXT"), ("lease_until", "TEXT"),
                ("last_error", "TEXT"), ("dead_letter_at", "TEXT"),
            )
            if self._is_postgres:
                for name, declaration in outbox_columns:
                    cur.execute(
                        f"ALTER TABLE foliant_domain_outbox ADD COLUMN IF NOT EXISTS "
                        f"{name} {declaration}"
                    )
            else:
                cur.execute("PRAGMA table_info(foliant_domain_outbox)")
                existing_outbox = {str(row[1]) for row in cur.fetchall()}
                for name, declaration in outbox_columns:
                    if name not in existing_outbox:
                        cur.execute(
                            f"ALTER TABLE foliant_domain_outbox ADD COLUMN {name} {declaration}"
                        )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _columns() -> tuple[str, ...]:
        return (
            "run_id", "actor_id", "capability", "run_kind", "mode", "idempotency_key",
            "request_hash", "request_payload", "request_id", "status", "cancellable",
            "cancel_requested", "resource_uri", "summary", "result_payload", "provenance",
            "warnings", "error_code", "created_at", "started_at", "completed_at", "failed_at",
            "worker_id", "fencing_token", "lease_until", "heartbeat_at", "attempt", "max_attempts",
            "next_attempt_at", "timeout_seconds", "updated_at",
        )

    @classmethod
    def _decode_row(cls, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        if hasattr(row, "keys"):
            # sqlite3.Row iterates values, unlike a normal Mapping; keys() is required.
            value = {key: row[key] for key in row.keys()}  # noqa: SIM118
        else:
            value = dict(zip(cls._columns(), row))
        for key, fallback in (("request_payload", {}), ("result_payload", None),
                              ("provenance", {}), ("warnings", [])):
            raw = value.get(key)
            if isinstance(raw, (dict, list)) or raw is None:
                value[key] = fallback if raw is None and fallback is not None else raw
            else:
                try:
                    value[key] = json.loads(raw)
                except (TypeError, ValueError):
                    value[key] = fallback
        value["cancellable"] = bool(value.get("cancellable"))
        value["cancel_requested"] = bool(value.get("cancel_requested"))
        value["attempt"] = int(value.get("attempt") or 0)
        value["max_attempts"] = int(value.get("max_attempts") or 2)
        value["timeout_seconds"] = int(value.get("timeout_seconds") or 1800)
        return value

    def _select(self, cur: Any, where: str, params: Iterable[Any]) -> dict[str, Any] | None:
        cur.execute(f"SELECT {','.join(self._columns())} FROM foliant_runs WHERE {where}", tuple(params))
        return self._decode_row(cur.fetchone())

    def get(self, run_id: str) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            return self._select(conn.cursor(), "run_id=?", (str(run_id),))
        finally:
            conn.close()

    def get_by_idempotency(self, actor_id: str, capability: str,
                           idempotency_key: str) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            return self._select(
                conn.cursor(), "actor_id=? AND capability=? AND idempotency_key=?",
                (actor_id, capability, idempotency_key),
            )
        finally:
            conn.close()

    def count_active(self, actor_id: str) -> int:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE foliant_run_attempts SET status='expired',completed_at=?,
                   error_code='worker_lease_expired'
                   WHERE status='running' AND EXISTS (
                     SELECT 1 FROM foliant_runs r
                     WHERE r.run_id=foliant_run_attempts.run_id
                       AND r.fencing_token=foliant_run_attempts.fencing_token
                       AND r.status='running' AND r.lease_until IS NOT NULL AND r.lease_until<?
                   )""",
                (now_value, now_value),
            )
            cur.execute(
                "SELECT COUNT(*) FROM foliant_runs WHERE actor_id=? AND status IN ('queued','running')",
                (actor_id,),
            )
            return int((cur.fetchone() or [0])[0] or 0)
        finally:
            conn.close()

    def create_or_get(
        self,
        *,
        actor_id: str,
        capability: str,
        run_kind: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        request_id: str = "",
        resource_uri_factory: Callable[[str], str],
        max_active: int | None = None,
        timeout_seconds: int = 1800,
        max_attempts: int = 2,
    ) -> CreateRunResult:
        request_digest = payload_hash(request_payload)
        run_id = uuid.uuid4().hex
        resource_uri = resource_uri_factory(run_id)
        created_at = now_iso()
        conn = self.connect()
        try:
            cur = conn.cursor()
            if self._is_postgres:
                # Serialize quota + create per actor without a separate lock table.
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (actor_id,))
            existing = self._select(
                cur, "actor_id=? AND capability=? AND idempotency_key=?",
                (actor_id, capability, idempotency_key),
            )
            if existing:
                if existing["request_hash"] != request_digest:
                    raise IdempotencyConflict("idempotency key conflicts with a different request")
                conn.rollback()
                return CreateRunResult(existing, False)
            if max_active is not None:
                cur.execute(
                    "SELECT COUNT(*) FROM foliant_runs WHERE actor_id=? "
                    "AND status IN ('queued','running')",
                    (actor_id,),
                )
                if int((cur.fetchone() or [0])[0] or 0) >= max(1, int(max_active)):
                    raise RunQuotaExceeded("active preview Run quota exceeded")
            cur.execute(
                """INSERT INTO foliant_runs
                   (run_id,actor_id,capability,run_kind,mode,idempotency_key,request_hash,
                    request_payload,request_id,status,cancellable,cancel_requested,resource_uri,
                    summary,result_payload,provenance,warnings,error_code,created_at,
                    max_attempts,next_attempt_at,timeout_seconds,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'queued',0,0,?,'',NULL,'{}','[]',NULL,?,?,?,?,?)""",
                (
                    run_id, actor_id, capability, run_kind, "preview", idempotency_key,
                    request_digest, canonical_json(request_payload), request_id, resource_uri,
                    created_at, max(1, min(5, int(max_attempts))), created_at,
                    max(30, min(7200, int(timeout_seconds))), created_at,
                ),
            )
            conn.commit()
        except (IdempotencyConflict, RunQuotaExceeded):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            existing = self.get_by_idempotency(actor_id, capability, idempotency_key)
            if existing and existing["request_hash"] == request_digest:
                return CreateRunResult(existing, False)
            if existing:
                raise IdempotencyConflict("idempotency key conflicts with a different request")
            raise
        finally:
            conn.close()
        return CreateRunResult(self.get(run_id) or {}, True)

    def claim_next(self, worker_id: str, *, lease_seconds: int = 120) -> dict[str, Any] | None:
        """Atomically lease one queued Run; expired leases are retried or failed first."""
        now = datetime.now().astimezone()
        now_value = now.isoformat(timespec="seconds")
        lease_until = (now + timedelta(seconds=max(30, int(lease_seconds)))).isoformat(
            timespec="seconds"
        )
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE foliant_runs SET status='queued',worker_id=NULL,fencing_token=NULL,lease_until=NULL,
                   heartbeat_at=NULL,next_attempt_at=?,updated_at=?
                   WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?
                     AND cancel_requested=0 AND attempt<max_attempts""",
                (now_value, now_value, now_value),
            )
            cur.execute(
                """UPDATE foliant_runs SET status='cancelled',cancellable=0,
                   completed_at=?,updated_at=?
                   WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?
                     AND cancel_requested=1""",
                (now_value, now_value, now_value),
            )
            cur.execute(
                """UPDATE foliant_runs SET status='failed',cancellable=0,
                   error_code='worker_lease_expired',failed_at=?,updated_at=?
                   WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?
                     AND attempt>=max_attempts""",
                (now_value, now_value, now_value),
            )
            suffix = " FOR UPDATE SKIP LOCKED" if self._is_postgres else ""
            cur.execute(
                "SELECT run_id FROM foliant_runs WHERE status='queued' AND cancel_requested=0 "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                f"ORDER BY created_at ASC LIMIT 1{suffix}",
                (now_value,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
            run_id = str(row[0])
            fencing_token = uuid.uuid4().hex
            cur.execute(
                """UPDATE foliant_runs SET status='running',cancellable=1,worker_id=?,
                   fencing_token=?,lease_until=?,heartbeat_at=?,attempt=attempt+1,
                   started_at=COALESCE(started_at,?),updated_at=?
                   WHERE run_id=? AND status='queued' AND cancel_requested=0""",
                (worker_id, fencing_token, lease_until, now_value,
                 now_value, now_value, run_id),
            )
            cur.execute(
                """INSERT INTO foliant_run_attempts
                   (run_id,attempt,worker_id,fencing_token,status,started_at,heartbeat_at)
                   SELECT run_id,attempt,worker_id,fencing_token,'running',?,?
                   FROM foliant_runs WHERE run_id=? AND worker_id=? AND fencing_token=?
                   ON CONFLICT(run_id,attempt) DO NOTHING""",
                (now_value, now_value, run_id, worker_id, fencing_token),
            )
            claimed = self._select(cur, "run_id=? AND worker_id=?", (run_id, worker_id))
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def heartbeat(self, run_id: str, worker_id: str, *, fencing_token: str | None = None,
                  lease_seconds: int = 120) -> bool:
        now = datetime.now().astimezone()
        now_value = now.isoformat(timespec="seconds")
        lease_until = (now + timedelta(seconds=max(30, int(lease_seconds)))).isoformat(
            timespec="seconds"
        )
        conn = self.connect()
        try:
            cur = conn.cursor()
            fence_clause = " AND fencing_token=?" if fencing_token else ""
            params = (now_value, lease_until, now_value, run_id, worker_id) + (
                (fencing_token,) if fencing_token else ()
            )
            cur.execute(
                """UPDATE foliant_runs SET heartbeat_at=?,lease_until=?,updated_at=?
                   WHERE run_id=? AND worker_id=? AND status='running' AND cancel_requested=0"""
                + fence_clause,
                params,
            )
            changed = cur.rowcount == 1
            if changed and fencing_token:
                cur.execute(
                    """UPDATE foliant_run_attempts SET heartbeat_at=?
                       WHERE run_id=? AND worker_id=? AND fencing_token=? AND status='running'""",
                    (now_value, run_id, worker_id, fencing_token),
                )
            conn.commit()
            return changed
        finally:
            conn.close()

    def mark_running(self, run_id: str) -> bool:
        now = now_iso()
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE foliant_runs SET status='running',cancellable=0,started_at=?,updated_at=?
                   WHERE run_id=? AND status='queued' AND cancel_requested=0""",
                (now, now, run_id),
            )
            changed = cur.rowcount == 1
            conn.commit()
            return changed
        finally:
            conn.close()

    def complete(self, run_id: str, result: dict[str, Any], *, event_type: str,
                 worker_id: str | None = None, fencing_token: str | None = None) -> bool:
        now = now_iso()
        summary = str(result.get("summary") or "")[:2000]
        provenance = clean_json(result.get("provenance") or {})
        warnings = clean_json(result.get("warnings") or [])
        conn = self.connect()
        try:
            cur = conn.cursor()
            owner_clause = " AND worker_id=?" if worker_id else ""
            if fencing_token:
                owner_clause += " AND fencing_token=?"
            params: tuple[Any, ...] = (
                summary, canonical_json(result), canonical_json(provenance),
                canonical_json(warnings), now, now, run_id,
            ) + ((worker_id,) if worker_id else ()) + ((fencing_token,) if fencing_token else ())
            cur.execute(
                """UPDATE foliant_runs SET status='complete',cancellable=0,summary=?,
                   result_payload=?,provenance=?,warnings=?,completed_at=?,updated_at=?
                   WHERE run_id=? AND status='running'""" + owner_clause,
                params,
            )
            changed = cur.rowcount == 1
            run = self._select(cur, "run_id=?", (run_id,))
            if changed and run:
                if run.get("run_kind") == "security-research":
                    from data.reliability_store import ReliabilityStore
                    symbol = str((run.get("request_payload") or {}).get("symbol", ""))
                    # Same commit as Run completion; never copy private report
                    # contents or actor IDs into the public Case namespace.
                    ReliabilityStore.append(cur, "case_execution", run_id, run["actor_id"], {
                        "symbol": symbol, "run_id": run_id, "status": "complete", "completed_at": now})
                data = result.get("data") if isinstance(result, dict) else None
                artifact = data.get("research_artifact") if isinstance(data, dict) else None
                if isinstance(artifact, dict) and artifact.get("artifact_id"):
                    cur.execute(
                        """INSERT INTO research_artifacts
                           (artifact_id,subject,artifact_kind,run_id,formal,schema_version,
                            payload_hash,payload,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(run_id,artifact_kind) DO NOTHING""",
                        (artifact["artifact_id"], artifact.get("subject", ""),
                         artifact.get("artifact_kind", "security-research"), run_id,
                         int(bool(artifact.get("formal"))), artifact.get("schema_version", ""),
                         artifact.get("payload_hash", ""), canonical_json(artifact),
                         artifact.get("created_at") or now),
                    )
                if fencing_token:
                    cur.execute(
                        """UPDATE foliant_run_attempts SET status='complete',completed_at=?
                           WHERE run_id=? AND worker_id=? AND fencing_token=? AND status='running'""",
                        (now, run_id, worker_id, fencing_token),
                    )
                event_payload = {
                    "event_id": uuid.uuid4().hex,
                    "run_id": run_id,
                    "status": "complete",
                    "mode": "preview",
                    "resource_uri": run["resource_uri"],
                    "created_at": now,
                }
                conn.execute(
                    """INSERT INTO foliant_domain_outbox
                       (event_id,run_id,event_type,resource_uri,payload,created_at)
                       VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,event_type) DO NOTHING""",
                    (
                        event_payload["event_id"], run_id, event_type, run["resource_uri"],
                        canonical_json(event_payload), now,
                    ),
                )
            conn.commit()
            return changed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def pending_outbox(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return metadata-only events awaiting delivery, oldest first."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT event_id,run_id,event_type,resource_uri,payload,created_at,attempts
                   FROM foliant_domain_outbox WHERE published_at IS NULL
                   ORDER BY created_at ASC LIMIT ?""",
                (max(1, min(200, int(limit))),),
            )
            rows = cur.fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                raw_payload = row[4]
                try:
                    decoded = raw_payload if isinstance(raw_payload, dict) else json.loads(raw_payload)
                except (TypeError, ValueError):
                    decoded = {}
                result.append({
                    "event_id": row[0], "run_id": row[1], "event_type": row[2],
                    "resource_uri": row[3], "payload": decoded,
                    "created_at": row[5], "attempts": int(row[6] or 0),
                })
            return result
        finally:
            conn.close()

    def claim_outbox(self, worker_id: str, *, lease_seconds: int = 60) -> dict[str, Any] | None:
        now = datetime.now().astimezone()
        now_value = now.isoformat(timespec="seconds")
        lease_until = (now + timedelta(seconds=max(30, int(lease_seconds)))).isoformat(
            timespec="seconds"
        )
        conn = self.connect()
        try:
            cur = conn.cursor()
            suffix = " FOR UPDATE SKIP LOCKED" if self._is_postgres else ""
            cur.execute(
                """SELECT event_id FROM foliant_domain_outbox
                   WHERE published_at IS NULL AND dead_letter_at IS NULL AND attempts<10
                     AND (claimed_by IS NULL OR lease_until<?)
                   ORDER BY created_at ASC LIMIT 1""" + suffix,
                (now_value,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
            event_id = str(row[0])
            cur.execute(
                """UPDATE foliant_domain_outbox SET claimed_by=?,lease_until=?
                   WHERE event_id=? AND published_at IS NULL
                     AND (claimed_by IS NULL OR lease_until<?)""",
                (worker_id, lease_until, event_id, now_value),
            )
            cur.execute(
                """SELECT event_id,run_id,event_type,resource_uri,payload,created_at,attempts
                   FROM foliant_domain_outbox WHERE event_id=? AND claimed_by=?""",
                (event_id, worker_id),
            )
            event = cur.fetchone()
            conn.commit()
            if not event:
                return None
            raw = event[4]
            return {
                "event_id": event[0], "run_id": event[1], "event_type": event[2],
                "resource_uri": event[3],
                "payload": raw if isinstance(raw, dict) else json.loads(raw or "{}"),
                "created_at": event[5], "attempts": int(event[6] or 0),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_outbox_published(self, event_id: str, worker_id: str | None = None) -> bool:
        conn = self.connect()
        try:
            cur = conn.cursor()
            owner_clause = " AND claimed_by=?" if worker_id else ""
            cur.execute(
                """UPDATE foliant_domain_outbox SET published_at=?,attempts=attempts+1
                   WHERE event_id=? AND published_at IS NULL""" + owner_clause,
                (now_iso(), event_id) + ((worker_id,) if worker_id else ()),
            )
            changed = cur.rowcount == 1
            conn.commit()
            return changed
        finally:
            conn.close()

    def record_outbox_failure(self, event_id: str, error_code: str = "publish_failed",
                              worker_id: str | None = None) -> None:
        conn = self.connect()
        try:
            owner_clause = " AND claimed_by=?" if worker_id else ""
            conn.execute(
                """UPDATE foliant_domain_outbox SET attempts=attempts+1,claimed_by=NULL,
                   lease_until=NULL,last_error=?,
                   dead_letter_at=CASE WHEN attempts+1>=10 THEN ? ELSE dead_letter_at END
                   WHERE event_id=? AND published_at IS NULL""" + owner_clause,
                (str(error_code)[:80], now_iso(), event_id)
                + ((worker_id,) if worker_id else ()),
            )
            conn.commit()
        finally:
            conn.close()

    def fail(self, run_id: str, error_code: str, *, worker_id: str | None = None,
             fencing_token: str | None = None) -> bool:
        now = now_iso()
        conn = self.connect()
        try:
            owner_clause = " AND worker_id=?" if worker_id else ""
            if fencing_token:
                owner_clause += " AND fencing_token=?"
            params: tuple[Any, ...] = (str(error_code)[:80], now, now, run_id) + (
                (worker_id,) if worker_id else ()
            ) + ((fencing_token,) if fencing_token else ())
            cur = conn.cursor()
            cur.execute(
                """UPDATE foliant_runs SET status='failed',cancellable=0,error_code=?,
                   failed_at=?,updated_at=? WHERE run_id=? AND status IN ('queued','running')"""
                + owner_clause,
                params,
            )
            changed = cur.rowcount == 1
            if changed and fencing_token:
                cur.execute(
                    """UPDATE foliant_run_attempts
                       SET status='failed',completed_at=?,error_code=?
                       WHERE run_id=? AND worker_id=? AND fencing_token=? AND status='running'""",
                    (now, str(error_code)[:80], run_id, worker_id, fencing_token),
                )
            conn.commit()
            return changed
        finally:
            conn.close()

    def cancel(self, run_id: str, actor_id: str) -> bool:
        now = now_iso()
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE foliant_runs SET status='cancelled',cancel_requested=1,cancellable=0,
                   completed_at=?,updated_at=? WHERE run_id=? AND actor_id=? AND status='queued'""",
                (now, now, run_id, actor_id),
            )
            changed = cur.rowcount == 1
            if not changed:
                cur.execute(
                    """UPDATE foliant_runs SET cancel_requested=1,cancellable=0,updated_at=?
                       WHERE run_id=? AND actor_id=? AND status='running'""",
                    (now, run_id, actor_id),
                )
                changed = cur.rowcount == 1
            conn.commit()
            return changed
        finally:
            conn.close()

    def finalize_cancel(self, run_id: str, worker_id: str,
                        fencing_token: str | None = None) -> bool:
        now = now_iso()
        conn = self.connect()
        try:
            cur = conn.cursor()
            fence_clause = " AND fencing_token=?" if fencing_token else ""
            cur.execute(
                """UPDATE foliant_runs SET status='cancelled',cancellable=0,
                   completed_at=?,updated_at=?
                   WHERE run_id=? AND worker_id=? AND status='running' AND cancel_requested=1"""
                + fence_clause,
                (now, now, run_id, worker_id) + ((fencing_token,) if fencing_token else ()),
            )
            changed = cur.rowcount == 1
            if changed and fencing_token:
                cur.execute(
                    """UPDATE foliant_run_attempts SET status='cancelled',completed_at=?
                       WHERE run_id=? AND worker_id=? AND fencing_token=? AND status='running'""",
                    (now, run_id, worker_id, fencing_token),
                )
            conn.commit()
            return changed
        finally:
            conn.close()

    def recover_incomplete(self, *, stale_seconds: int = 21600) -> int:
        """Legacy maintenance hook: immediately reclaim stale, unleased pre-worker Runs."""
        now = now_iso()
        cutoff = (datetime.now().astimezone() - timedelta(
            seconds=max(300, int(stale_seconds))
        )).isoformat(timespec="seconds")
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE foliant_runs SET status='failed',cancellable=0,
                   error_code='worker_restarted',failed_at=?,updated_at=?
                   WHERE status='running' AND lease_until IS NULL AND updated_at<?""",
                (now, now, cutoff),
            )
            changed = max(0, int(cur.rowcount or 0))
            conn.commit()
            return changed
        finally:
            conn.close()

    def append_progress(self, run_id: str, *, worker_id: str, fencing_token: str,
                        phase: str, current: int | None = None, total: int | None = None,
                        message_code: str = "") -> bool:
        """Append bounded metadata-only progress when the caller still owns the Run."""
        safe_phase = str(phase or "working")[:48]
        safe_message = str(message_code or "")[:80]
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT attempt FROM foliant_runs WHERE run_id=? AND worker_id=?
                   AND fencing_token=? AND status='running'""",
                (run_id, worker_id, fencing_token),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return False
            attempt = int(row[0] or 0)
            cur.execute(
                """INSERT INTO foliant_run_progress
                   (event_id,run_id,attempt,phase,current_value,total_value,message_code,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, run_id, attempt, safe_phase,
                 None if current is None else int(current),
                 None if total is None else max(0, int(total)), safe_message, now_iso()),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def progress(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT attempt,phase,current_value,total_value,message_code,created_at
                   FROM foliant_run_progress WHERE run_id=?
                   ORDER BY created_at ASC LIMIT ?""",
                (run_id, max(1, min(200, int(limit)))),
            )
            return [{
                "attempt": int(row[0]), "phase": row[1], "current": row[2],
                "total": row[3], "message_code": row[4] or "", "created_at": row[5],
            } for row in cur.fetchall()]
        finally:
            conn.close()

    def get_write_result(self, actor_id: str, action: str,
                         idempotency_key: str, request_payload: Any) -> dict[str, Any] | None:
        request_digest = payload_hash(request_payload)
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT request_hash,response_payload FROM foliant_write_idempotency
                   WHERE actor_id=? AND action=? AND idempotency_key=?""",
                (actor_id, action, idempotency_key),
            )
            row = cur.fetchone()
            if not row:
                return None
            if str(row[0]) != request_digest:
                raise IdempotencyConflict("idempotency key conflicts with a different request")
            return json.loads(row[1]) if not isinstance(row[1], dict) else dict(row[1])
        finally:
            conn.close()

    def claim_write(self, actor_id: str, action: str, idempotency_key: str,
                    request_payload: Any) -> tuple[bool, dict[str, Any] | None]:
        """Claim a write key before its side effect; concurrent callers observe one owner."""
        request_digest = payload_hash(request_payload)
        stale_before = (
            datetime.now().astimezone() - timedelta(minutes=15)
        ).isoformat(timespec="seconds")
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """DELETE FROM foliant_write_idempotency
                   WHERE actor_id=? AND action=? AND idempotency_key=? AND request_hash=?
                     AND response_payload=? AND created_at<?""",
                (
                    actor_id, action, idempotency_key, request_digest,
                    canonical_json({"status": "processing"}), stale_before,
                ),
            )
            cur.execute(
                """INSERT INTO foliant_write_idempotency
                   (actor_id,action,idempotency_key,request_hash,response_payload,created_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(actor_id,action,idempotency_key) DO NOTHING""",
                (
                    actor_id, action, idempotency_key, request_digest,
                    canonical_json({"status": "processing"}), now_iso(),
                ),
            )
            claimed = cur.rowcount == 1
            conn.commit()
        finally:
            conn.close()
        if claimed:
            return True, None
        existing = self.get_write_result(
            actor_id, action, idempotency_key, request_payload
        )
        return False, existing

    def release_write_claim(self, actor_id: str, action: str,
                            idempotency_key: str, request_payload: Any) -> None:
        """Release only an unfinished claim so a failed attempt can be retried safely."""
        conn = self.connect()
        try:
            conn.execute(
                """DELETE FROM foliant_write_idempotency
                   WHERE actor_id=? AND action=? AND idempotency_key=? AND request_hash=?
                     AND response_payload=?""",
                (
                    actor_id, action, idempotency_key, payload_hash(request_payload),
                    canonical_json({"status": "processing"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def save_write_result(self, actor_id: str, action: str, idempotency_key: str,
                          request_payload: Any, response_payload: Any) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """INSERT INTO foliant_write_idempotency
                   (actor_id,action,idempotency_key,request_hash,response_payload,created_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(actor_id,action,idempotency_key) DO NOTHING""",
                (
                    actor_id, action, idempotency_key, payload_hash(request_payload),
                    canonical_json(response_payload), now_iso(),
                ),
            )
            conn.execute(
                """UPDATE foliant_write_idempotency SET response_payload=?
                   WHERE actor_id=? AND action=? AND idempotency_key=? AND request_hash=?""",
                (
                    canonical_json(response_payload), actor_id, action, idempotency_key,
                    payload_hash(request_payload),
                ),
            )
            conn.commit()
        finally:
            conn.close()
