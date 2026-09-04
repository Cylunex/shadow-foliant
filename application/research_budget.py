"""Research-only LLM admission/cache using the existing artifact registry.

Reservation is persisted before spending tokens. A crash consumes the reservation;
retrying cannot silently spend again. Actual token telemetry stays in llm_usage.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from application.results import payload_hash


def committee_call(store, *, prompt, system, call):
    from jobs.schedule_policy import weekend_llm_allowed
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if not weekend_llm_allowed(now, estimated_minutes=5) or now.hour * 60 + now.minute + 5 > 23 * 60 + 30:
        return {"status": "outside_research_window"}
    week = f"{now.isocalendar().year}-{now.isocalendar().week:02d}"
    identity = payload_hash({"week": week, "kind": "committee", "prompt": prompt})
    subject = f"committee-budget:{week}"
    conn = store.connect()
    try:
        cur = conn.cursor()
        if store._is_postgres:
            cur.execute("SELECT pg_advisory_xact_lock(1936482717)")
        else:
            cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT payload FROM research_artifacts WHERE run_id=? AND artifact_kind='committee-result'", (identity,))
        row = cur.fetchone()
        if row:
            conn.rollback()
            return {**json.loads(row[0]), "cache_hit": True}
        cur.execute("SELECT COUNT(*) FROM research_artifacts WHERE subject=? AND artifact_kind='committee-reservation'", (subject,))
        if cur.fetchone()[0] >= 1:
            conn.rollback()
            return {"status": "weekly_budget_used", "reserved_output_tokens": 1800}
        reservation = {"status": "reserved", "max_output_tokens": 1800,
                       "max_calls_per_week": 1, "created_at": now.isoformat()}
        cur.execute("INSERT INTO research_artifacts "
                    "(artifact_id,subject,artifact_kind,run_id,formal,schema_version,payload_hash,payload,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", ("budget_" + identity, subject, "committee-reservation", identity,
                    0, "research-budget-v1", payload_hash(reservation), json.dumps(reservation), now.isoformat()))
        conn.commit()
    finally:
        conn.close()
    try:
        text, provider = call([{ "role": "system", "content": system},
                               {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
                              temperature=.1, max_tokens=1800, timeout=90, call_type="strategy_policy_committee")
        result = {"status": "complete", "text": text, "provider": provider, "cache_hit": False}
    except Exception as exc:
        result = {"status": "llm_unavailable", "error_category": type(exc).__name__, "cache_hit": False}
    conn = store.connect()
    try:
        conn.execute("INSERT INTO research_artifacts "
                     "(artifact_id,subject,artifact_kind,run_id,formal,schema_version,payload_hash,payload,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,artifact_kind) DO NOTHING",
                     ("committee_" + identity, subject, "committee-result", identity, 0, "research-budget-v1",
                      payload_hash(result), json.dumps(result, ensure_ascii=False), now.isoformat()))
        conn.commit()
    finally:
        conn.close()
    return result
