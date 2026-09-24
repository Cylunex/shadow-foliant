"""Private, slot-scoped QQ delivery ledger. No message body or webhook is stored."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
SLOT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T(?:10:15|11:25|14:35|20:45)\+08:00$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class ScheduledNotificationService:
    def __init__(self, store: Any = None, clock: Callable[[], datetime] | None = None):
        if store is None:
            from data.research_store import ResearchStore
            store = ResearchStore(ensure_schema=False)
        self.store = store
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def _now(self) -> datetime:
        return self.clock().astimezone(SHANGHAI)

    @staticmethod
    def _slot(slot: str) -> str:
        if not SLOT_PATTERN.fullmatch(slot or ""):
            raise ValueError("scheduled_notification_slot_invalid")
        return slot

    def claim(self, *, slot: str, payload_hash: str, original_lines: int,
              delivered_lines: int, category: str, version: str,
              actor_id: str) -> dict[str, Any]:
        slot = self._slot(slot)
        now = self._now()
        planned = datetime.fromisoformat(slot)
        if not timedelta(0) <= now - planned <= timedelta(minutes=60):
            raise ValueError("scheduled_notification_slot_outside_window")
        if not HASH_PATTERN.fullmatch(payload_hash or ""):
            raise ValueError("scheduled_notification_hash_invalid")
        if not (1 <= original_lines <= 1000 and 1 <= delivered_lines <= 8):
            raise ValueError("scheduled_notification_line_count_invalid")
        if category != "report" or not VERSION_PATTERN.fullmatch(version or ""):
            raise ValueError("scheduled_notification_metadata_invalid")
        if not actor_id:
            raise PermissionError("actor_required")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""INSERT INTO scheduled_notification_deliveries
                (notification_slot,actor_id,payload_hash,original_lines,delivered_lines,
                 category,version,claimed_at,delivery_status)
                VALUES (?,?,?,?,?,?,?,?,'claimed')
                ON CONFLICT(notification_slot) DO NOTHING""",
                (slot, actor_id, payload_hash, original_lines, delivered_lines,
                 category, version, now.isoformat(timespec="seconds")))
            cur.execute("""SELECT actor_id,payload_hash,delivery_status,attempted_at,
                               delivered_at,http_status,error_code
                           FROM scheduled_notification_deliveries
                           WHERE notification_slot=?""", (slot,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError("scheduled_notification_claim_missing")
            if row[0] != actor_id:
                raise PermissionError("scheduled_notification_actor_mismatch")
            status = str(row[2])
            reason = (
                "prior_sent" if status == "delivered" else
                "attempt_outcome_known" if status == "failed" else
                "attempt_outcome_unknown" if status in {"sending", "unknown"} else
                "payload_changed_before_attempt" if row[1] != payload_hash else None
            )
            if reason:
                cur.execute("""UPDATE scheduled_notification_deliveries
                               SET suppression_reason=?,suppressed_count=suppressed_count+1
                               WHERE notification_slot=?""", (reason, slot))
            conn.commit()
            return {
                "notification_slot": slot, "delivery_status": status,
                "should_send": status == "claimed" and row[3] is None
                               and row[1] == payload_hash,
                "prior_sent": status == "delivered",
                "suppression_reason": reason,
                "payload_matches": row[1] == payload_hash,
                "attempted_at": row[3], "delivered_at": row[4],
                "http_status": row[5], "error_code": row[6],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start(self, *, slot: str, payload_hash: str, actor_id: str) -> dict[str, Any]:
        slot = self._slot(slot)
        current = self._now()
        if not timedelta(0) <= current - datetime.fromisoformat(slot) <= timedelta(minutes=60):
            raise ValueError("scheduled_notification_slot_outside_window")
        now = current.isoformat(timespec="seconds")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""UPDATE scheduled_notification_deliveries
                           SET delivery_status='sending',attempted_at=?
                           WHERE notification_slot=? AND payload_hash=? AND actor_id=?
                             AND delivery_status='claimed' AND attempted_at IS NULL""",
                        (now, slot, payload_hash, actor_id))
            started = int(cur.rowcount or 0) == 1
            conn.commit()
            return {"started": started, "notification_slot": slot}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finish(self, *, slot: str, payload_hash: str, actor_id: str,
               status: str, http_status: int | None = None,
               error_code: str | None = None) -> dict[str, Any]:
        slot = self._slot(slot)
        if status not in {"delivered", "failed", "unknown"}:
            raise ValueError("scheduled_notification_status_invalid")
        if http_status is not None and not 100 <= http_status <= 599:
            raise ValueError("scheduled_notification_http_status_invalid")
        if error_code and not re.fullmatch(r"[a-z0-9_]{1,80}", error_code):
            raise ValueError("scheduled_notification_error_code_invalid")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""UPDATE scheduled_notification_deliveries
                           SET delivery_status=?,delivered_at=?,http_status=?,error_code=?
                           WHERE notification_slot=? AND payload_hash=? AND actor_id=?
                             AND delivery_status='sending'""",
                        (status, self._now().isoformat(timespec="seconds")
                         if status == "delivered" else None, http_status, error_code,
                         slot, payload_hash, actor_id))
            recorded = int(cur.rowcount or 0) == 1
            conn.commit()
            return {"recorded": recorded, "delivery_status": status}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def audit(self, *, actor_id: str, days: int = 14) -> list[dict[str, Any]]:
        if not actor_id:
            raise PermissionError("actor_required")
        days = max(1, min(int(days), 14))
        cutoff = (self._now() - timedelta(days=days)).date().isoformat()
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT notification_slot,claimed_at,attempted_at,delivered_at,
                                  payload_hash,original_lines,delivered_lines,category,
                                  http_status,error_code,delivery_status,version,
                                  suppression_reason,suppressed_count
                           FROM scheduled_notification_deliveries
                           WHERE notification_slot>=?
                           ORDER BY notification_slot DESC LIMIT 56""", (cutoff,))
            rows = cur.fetchall()
            return [dict(zip(("notification_slot", "claimed_at", "attempted_at",
                              "delivered_at", "payload_hash", "original_lines",
                              "delivered_lines", "category", "http_status",
                              "error_code", "delivery_status", "version",
                              "suppression_reason", "suppressed_count"), row))
                    for row in rows]
        finally:
            conn.close()
