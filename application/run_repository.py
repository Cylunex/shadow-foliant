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
                updated_at TEXT NOT NULL,
                UNIQUE(actor_id, capability, idempotency_key)
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
            "CREATE INDEX IF NOT EXISTS idx_foliant_outbox_pending ON foliant_domain_outbox(published_at, created_at)",
        ]
        conn = self.connect()
        try:
            cur = conn.cursor()
            for statement in statements:
                cur.execute(statement)
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
            "updated_at",
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
    ) -> CreateRunResult:
        request_digest = payload_hash(request_payload)
        existing = self.get_by_idempotency(actor_id, capability, idempotency_key)
        if existing:
            if existing["request_hash"] != request_digest:
                raise IdempotencyConflict("idempotency key conflicts with a different request")
            return CreateRunResult(existing, False)

        run_id = uuid.uuid4().hex
        resource_uri = resource_uri_factory(run_id)
        created_at = now_iso()
        conn = self.connect()
        try:
            conn.execute(
                """INSERT INTO foliant_runs
                   (run_id,actor_id,capability,run_kind,mode,idempotency_key,request_hash,
                    request_payload,request_id,status,cancellable,cancel_requested,resource_uri,
                    summary,result_payload,provenance,warnings,error_code,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'queued',0,0,?,'',NULL,'{}','[]',NULL,?,?)""",
                (
                    run_id, actor_id, capability, run_kind, "preview", idempotency_key,
                    request_digest, canonical_json(request_payload), request_id, resource_uri,
                    created_at, created_at,
                ),
            )
            conn.commit()
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

    def complete(self, run_id: str, result: dict[str, Any], *, event_type: str) -> None:
        now = now_iso()
        summary = str(result.get("summary") or "")[:2000]
        provenance = clean_json(result.get("provenance") or {})
        warnings = clean_json(result.get("warnings") or [])
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE foliant_runs SET status='complete',cancellable=0,summary=?,
                   result_payload=?,provenance=?,warnings=?,completed_at=?,updated_at=?
                   WHERE run_id=? AND status='running'""",
                (
                    summary, canonical_json(result), canonical_json(provenance),
                    canonical_json(warnings), now, now, run_id,
                ),
            )
            changed = cur.rowcount == 1
            run = self._select(cur, "run_id=?", (run_id,))
            if changed and run:
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

    def mark_outbox_published(self, event_id: str) -> bool:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE foliant_domain_outbox SET published_at=?,attempts=attempts+1
                   WHERE event_id=? AND published_at IS NULL""",
                (now_iso(), event_id),
            )
            changed = cur.rowcount == 1
            conn.commit()
            return changed
        finally:
            conn.close()

    def record_outbox_failure(self, event_id: str) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """UPDATE foliant_domain_outbox SET attempts=attempts+1
                   WHERE event_id=? AND published_at IS NULL""",
                (event_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def fail(self, run_id: str, error_code: str) -> None:
        now = now_iso()
        conn = self.connect()
        try:
            conn.execute(
                """UPDATE foliant_runs SET status='failed',cancellable=0,error_code=?,
                   failed_at=?,updated_at=? WHERE run_id=? AND status IN ('queued','running')""",
                (str(error_code)[:80], now, now, run_id),
            )
            conn.commit()
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
            conn.commit()
            return changed
        finally:
            conn.close()

    def recover_incomplete(self, *, stale_seconds: int = 21600) -> int:
        """Converge only stale abandoned work, without killing another live Foliant process."""
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
                   WHERE status IN ('queued','running') AND updated_at<?""",
                (now, now, cutoff),
            )
            changed = max(0, int(cur.rowcount or 0))
            conn.commit()
            return changed
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
