"""Append-only domain revisions and durable work admission, not a second scheduler.

The existing Run worker owns execution. This journal owns research facts, revisions,
owner isolation and idempotency. A revision is committed before a caller spends work.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from application.results import payload_hash

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS research_reliability_records (
        kind TEXT NOT NULL, object_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        revision INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(kind,object_id,owner_id,revision))""",
    """CREATE INDEX IF NOT EXISTS idx_reliability_owner
        ON research_reliability_records(owner_id,kind,created_at)""",
    """CREATE TABLE IF NOT EXISTS research_reliability_work (
        work_id TEXT PRIMARY KEY, kind TEXT NOT NULL, priority INTEGER NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL, available_at TEXT NOT NULL,
        payload TEXT NOT NULL, updated_at TEXT NOT NULL)""",
]


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def canonical_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc).isoformat()


class ReliabilityStore:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def transaction(self):
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            if self.store._is_postgres:
                cur.execute("SELECT pg_advisory_xact_lock(1936482720)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def read(cur, kind, identity, owner):
        cur.execute("SELECT payload,revision FROM research_reliability_records "
                    "WHERE kind=? AND object_id=? AND owner_id=? ORDER BY revision DESC LIMIT 1",
                    (kind, identity, owner))
        row = cur.fetchone()
        return {**json.loads(row[0]), "revision": row[1]} if row else None

    def get(self, kind, identity, owner="research"):
        conn = self.store.connect()
        try:
            return self.read(conn.cursor(), kind, identity, owner)
        finally:
            conn.close()

    @staticmethod
    def append(cur, kind, identity, owner, value, previous=None):
        if not owner or not identity:
            raise ValueError("owner_and_identity_required")
        revision = (previous or {}).get("revision", 0) + 1
        value = {**value, "object_id": identity, "revision": revision}
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)
        if len(raw.encode()) > 262144:
            raise ValueError("research_record_too_large")
        cur.execute("INSERT INTO research_reliability_records VALUES (?,?,?,?,?,?)",
                    (kind, identity, owner, revision, raw, utcnow()))
        return value

    def put(self, kind, identity, value, *, owner="research", expected_revision=0):
        with self.transaction() as cur:
            old = self.read(cur, kind, identity, owner)
            if (old or {}).get("revision", 0) != expected_revision:
                raise ValueError("revision_conflict")
            return self.append(cur, kind, identity, owner, value, old)

    def once(self, kind, identity, value, *, owner="research"):
        with self.transaction() as cur:
            return self.read(cur, kind, identity, owner) or self.append(cur, kind, identity, owner, value)

    def list(self, kind, *, owner="research", limit=100):
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT r.payload FROM research_reliability_records r WHERE r.kind=? AND r.owner_id=? "
                        "AND NOT EXISTS (SELECT 1 FROM research_reliability_records n WHERE n.kind=r.kind "
                        "AND n.object_id=r.object_id AND n.owner_id=r.owner_id AND n.revision>r.revision) "
                        "ORDER BY r.created_at DESC LIMIT ?", (kind, owner, min(max(int(limit), 1), 2000)))
            return [json.loads(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()

    def enqueue(self, kind, key, payload, *, priority=2, available_at=None):
        identity = payload_hash({"kind": kind, "key": key})
        with self.transaction() as cur:
            cur.execute("INSERT INTO research_reliability_work VALUES (?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(work_id) DO NOTHING", (identity, kind, priority, "pending", 0,
                        canonical_time(available_at) if available_at else utcnow(), json.dumps(payload, allow_nan=False), utcnow()))
        return identity

    def claim(self, kind, *, limit=160, now=None, max_attempts=3):
        # No 'running' lease: reservation consumes one attempt; after a crash the
        # existing Run can retry the same id after its bounded backoff.
        from datetime import timedelta
        now = canonical_time(now) if now else utcnow()
        later = (datetime.fromisoformat(now) + timedelta(minutes=15)).isoformat()
        with self.transaction() as cur:
            cur.execute("SELECT work_id,payload,attempts FROM research_reliability_work "
                        "WHERE kind=? AND state='pending' AND available_at<=? AND attempts<? "
                        "ORDER BY attempts,priority,updated_at,work_id LIMIT ?", (kind, now, max_attempts, min(limit, 160)))
            rows = cur.fetchall()
            for identity, _, _ in rows:
                cur.execute("UPDATE research_reliability_work SET attempts=attempts+1,available_at=?,updated_at=? "
                            "WHERE work_id=?", (later, now, identity))
            return [{"work_id": i, "attempt": a + 1, **json.loads(p)} for i, p, a in rows]

    def finish(self, identity, *, state="complete"):
        if state not in {"complete", "excluded"}:
            raise ValueError("invalid_work_state")
        with self.transaction() as cur:
            cur.execute("UPDATE research_reliability_work SET state=?,updated_at=? WHERE work_id=?",
                        (state, utcnow(), identity))

    def work_status(self, kind):
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT CASE WHEN state='pending' AND attempts>=3 THEN 'exhausted' ELSE state END,COUNT(*) "
                        "FROM research_reliability_work WHERE kind=? GROUP BY "
                        "CASE WHEN state='pending' AND attempts>=3 THEN 'exhausted' ELSE state END", (kind,))
            return dict(cur.fetchall())
        finally:
            conn.close()
