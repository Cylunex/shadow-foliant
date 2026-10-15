"""Private, slot-scoped QQ delivery ledger. No message body or webhook is stored."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from application.scheduled_report_contract import (REPORT_MAX_DELIVERED_LINES,
    REPORT_MAX_ORIGINAL_LINES, REPORT_MAX_PARTS)
from application.scheduled_pick_policy import PICK_POLICY_VERSION, decide_selection


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
              actor_id: str, part_count: int = 1,
              pick_fingerprint: str | None = None,
              pick_origin_fingerprint: str | None = None,
              pick_selection_run_id: str | None = None,
              pick_research_id: str | None = None,
              pick_sources: str = "", pick_included: bool = False) -> dict[str, Any]:
        slot = self._slot(slot)
        now = self._now()
        planned = datetime.fromisoformat(slot)
        if not timedelta(0) <= now - planned <= timedelta(minutes=60):
            raise ValueError("scheduled_notification_slot_outside_window")
        if not HASH_PATTERN.fullmatch(payload_hash or ""):
            raise ValueError("scheduled_notification_hash_invalid")
        if (type(original_lines) is not int or type(delivered_lines) is not int
                or not (1 <= original_lines <= REPORT_MAX_ORIGINAL_LINES
                        and 1 <= delivered_lines <= REPORT_MAX_DELIVERED_LINES)
                or type(part_count) is not int or not 1 <= part_count <= REPORT_MAX_PARTS):
            raise ValueError("scheduled_notification_line_count_invalid")
        if category != "report" or not VERSION_PATTERN.fullmatch(version or ""):
            raise ValueError("scheduled_notification_metadata_invalid")
        if not actor_id:
            raise PermissionError("actor_required")
        if pick_fingerprint is not None and not HASH_PATTERN.fullmatch(pick_fingerprint):
            raise ValueError("scheduled_notification_pick_fingerprint_invalid")
        if pick_origin_fingerprint is not None and not HASH_PATTERN.fullmatch(pick_origin_fingerprint):
            raise ValueError("scheduled_notification_pick_origin_invalid")
        for identifier in (pick_selection_run_id, pick_research_id):
            if identifier is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", identifier):
                raise ValueError("scheduled_notification_pick_run_invalid")
        if (pick_sources and (not re.fullmatch(
                r"正式\+独立量化(?:\+外部研究)?", pick_sources)
                or pick_fingerprint is None)) or (pick_included and not (
                pick_fingerprint and pick_origin_fingerprint and pick_selection_run_id)):
            raise ValueError("scheduled_notification_pick_metadata_invalid")
        if pick_included and ("外部研究" in pick_sources) != bool(pick_research_id):
            raise ValueError("scheduled_notification_pick_research_invalid")
        if pick_included and not decide_selection(slot, {
                "verified": True, "fingerprint": pick_fingerprint,
                "origin_fingerprint": pick_origin_fingerprint,
                "sources": pick_sources}, self.selection_state(slot=slot, actor_id=actor_id)):
            raise ValueError("scheduled_notification_pick_policy_rejected")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""INSERT INTO scheduled_notification_deliveries
                (notification_slot,actor_id,payload_hash,original_lines,delivered_lines,
                 category,version,part_count,claimed_at,delivery_status,
                 pick_fingerprint,pick_origin_fingerprint,pick_selection_run_id,
                 pick_research_id,pick_sources,pick_included)
                VALUES (?,?,?,?,?,?,?,?,?,'claimed',?,?,?,?,?,?)
                ON CONFLICT(notification_slot) DO NOTHING""",
                (slot, actor_id, payload_hash, original_lines, delivered_lines,
                 category, version, part_count, now.isoformat(timespec="seconds"),
                 pick_fingerprint, pick_origin_fingerprint,
                 pick_selection_run_id, pick_research_id,
                 pick_sources, int(pick_included)))
            cur.execute("""SELECT actor_id,payload_hash,delivery_status,attempted_at,
                               delivered_at,http_status,error_code,part_count,accepted_part_count,
                               message_archive_status,message_archive_id
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
                "attempt_outcome_known" if status in {"failed", "partial"} else
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
                "part_count": row[7], "accepted_part_count": row[8],
                "message_archive_status": row[9], "message_archive_id": row[10],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def selection_state(self, *, slot: str, actor_id: str) -> dict[str, Any]:
        slot = self._slot(slot)
        if not actor_id:
            raise PermissionError("actor_required")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT notification_slot,pick_fingerprint,pick_origin_fingerprint,
                                  pick_selection_run_id,pick_research_id,pick_sources,
                                  pick_included,delivery_status,part_count,
                                  accepted_part_count,http_status
                           FROM scheduled_notification_deliveries
                           WHERE notification_slot>=? AND notification_slot<?
                           ORDER BY notification_slot""", (slot[:10], slot))
            rows = cur.fetchall()
        finally:
            conn.close()
        accepted = None
        morning = None
        uncertain = False
        for (prior_slot, fingerprint, origin, run_id, research_id, sources,
             included, status, total, count, http) in rows:
            if prior_slot.endswith("T10:15+08:00"):
                morning = (fingerprint, origin, run_id)
            if not included or not fingerprint:
                continue
            if count == total and http in {200, 204}:
                accepted = (fingerprint, origin, sources, prior_slot, run_id, research_id)
            elif status in {"sending", "unknown", "partial"} or count:
                uncertain = True
        return {"policy_version": PICK_POLICY_VERSION,
                "notification_slot": slot,
                "accepted_fingerprint": accepted[0] if accepted else None,
                "accepted_origin_fingerprint": accepted[1] if accepted else None,
                "accepted_sources": accepted[2] if accepted else None,
                "accepted_slot": accepted[3] if accepted else None,
                "accepted_selection_run_id": accepted[4] if accepted else None,
                "accepted_research_id": accepted[5] if accepted else None,
                "morning_fingerprint": morning[0] if morning else None,
                "morning_origin_fingerprint": morning[1] if morning else None,
                "morning_selection_run_id": morning[2] if morning else None,
                "uncertain_pick_attempt": uncertain}

    def record_silent(self, *, slot: str, actor_id: str) -> dict[str, Any]:
        slot = self._slot(slot)
        now = self._now()
        if not timedelta(0) <= now - datetime.fromisoformat(slot) <= timedelta(minutes=60):
            raise ValueError("scheduled_notification_slot_outside_window")
        if slot.endswith("T10:15+08:00"):
            raise ValueError("scheduled_notification_morning_cannot_be_silent")
        if not actor_id:
            raise PermissionError("actor_required")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT delivery_status,delivered_at,message_archive_status,
                                  message_archive_id
                           FROM scheduled_notification_deliveries
                           WHERE notification_slot=?""", (slot,))
            existing = cur.fetchone()
            if existing:
                return {"recorded": False, "notification_slot": slot,
                        "delivery_status": existing[0],
                        "prior_sent": existing[0] == "delivered",
                        "delivered_at": existing[1],
                        "message_archive_status": existing[2],
                        "message_archive_id": existing[3]}
            cur.execute("""INSERT INTO scheduled_notification_silent_slots
                           (notification_slot,actor_id,recorded_at,reason)
                           SELECT ?,?,?,? WHERE NOT EXISTS (
                               SELECT 1 FROM scheduled_notification_deliveries
                               WHERE notification_slot=?)
                           ON CONFLICT(notification_slot) DO NOTHING""",
                        (slot, actor_id, now.isoformat(timespec="seconds"),
                         "no_meaningful_report_content", slot))
            recorded = int(cur.rowcount or 0) == 1
            conn.commit()
            return {"recorded": recorded, "notification_slot": slot,
                    "delivery_status": "silent" if recorded else "unobserved"}
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
               error_code: str | None = None, accepted_part_count: int = 0,
               message_archive_status: str | None = None,
               message_archive_id: str | None = None,
               market_context: dict[str, Any] | None = None,
               account_context: dict[str, Any] | None = None) -> dict[str, Any]:
        slot = self._slot(slot)
        if status not in {"delivered", "failed", "partial", "unknown"}:
            raise ValueError("scheduled_notification_status_invalid")
        if http_status is not None and not 100 <= http_status <= 599:
            raise ValueError("scheduled_notification_http_status_invalid")
        if error_code and not re.fullmatch(r"[a-z0-9_]{1,80}", error_code):
            raise ValueError("scheduled_notification_error_code_invalid")
        if type(accepted_part_count) is not int or not 0 <= accepted_part_count <= REPORT_MAX_PARTS:
            raise ValueError("scheduled_notification_part_count_invalid")
        if message_archive_status is not None and message_archive_status not in {
                "recorded", "partial", "unknown", "unrecorded", "suppressed"}:
            raise ValueError("scheduled_notification_archive_status_invalid")
        if message_archive_id is not None and not re.fullmatch(r"[0-9a-f]{32}", message_archive_id):
            raise ValueError("scheduled_notification_archive_id_invalid")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT part_count FROM scheduled_notification_deliveries
                           WHERE notification_slot=? AND payload_hash=? AND actor_id=?
                             AND delivery_status='sending'""", (slot, payload_hash, actor_id))
            row = cur.fetchone()
            if row:
                total = int(row[0])
                # Legacy single-message callers did not report a part count.
                if status == "delivered" and total == 1 and accepted_part_count == 0:
                    accepted_part_count = 1
                if (accepted_part_count > total
                        or (status == "delivered" and accepted_part_count != total)
                        or (status == "partial" and not 0 < accepted_part_count < total)):
                    raise ValueError("scheduled_notification_part_result_invalid")
            cur.execute("""UPDATE scheduled_notification_deliveries
                           SET delivery_status=?,delivered_at=?,http_status=?,error_code=?,
                               accepted_part_count=?,message_archive_status=?,message_archive_id=?
                           WHERE notification_slot=? AND payload_hash=? AND actor_id=?
                             AND delivery_status='sending' AND part_count>=?""",
                        (status, self._now().isoformat(timespec="seconds")
                         if status == "delivered" else None, http_status, error_code,
                         accepted_part_count, message_archive_status,
                         message_archive_id, slot, payload_hash, actor_id,
                         accepted_part_count))
            recorded = int(cur.rowcount or 0) == 1
            conn.commit()
            market_recorded = False
            if (recorded and status == "delivered" and message_archive_status == "recorded"
                    and market_context):
                from application.market_risk_notice import report_reference
                reference = report_reference(market_context, slot[:10], self._now(),
                                             account_context)
                if reference:
                    try:
                        from jobs.jobs_hub import save_indicator_snapshot
                        save_indicator_snapshot("_scheduled_market_risk_reference", reference)
                        market_recorded = True
                    except Exception:
                        # Delivery is already final; expose the state gap without
                        # claiming a second attempt or sending a duplicate.
                        market_recorded = False
            return {"recorded": recorded, "delivery_status": status,
                    "market_reference_recorded": market_recorded}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def audit(self, *, actor_id: str, days: int = 14) -> list[dict[str, Any]]:
        if not actor_id:
            raise PermissionError("actor_required")
        days = max(1, min(int(days), 14))
        current = self._now()
        cutoff = (current.date() - timedelta(days=days - 1)).isoformat()
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT notification_slot,claimed_at,attempted_at,delivered_at,
                                  payload_hash,original_lines,delivered_lines,category,
                                  http_status,error_code,delivery_status,version,
                                  suppression_reason,suppressed_count,part_count,
                                  accepted_part_count,message_archive_status,
                                  message_archive_id,pick_fingerprint,
                                  pick_origin_fingerprint,pick_selection_run_id,
                                  pick_research_id,pick_sources,pick_included
                           FROM scheduled_notification_deliveries
                           WHERE notification_slot>=?
                           ORDER BY notification_slot DESC LIMIT 56""", (cutoff,))
            rows = cur.fetchall()
            fields = ("notification_slot", "claimed_at", "attempted_at",
                      "delivered_at", "payload_hash", "original_lines",
                      "delivered_lines", "category", "http_status",
                      "error_code", "delivery_status", "version",
                      "suppression_reason", "suppressed_count", "part_count",
                      "accepted_part_count", "message_archive_status",
                      "message_archive_id", "pick_fingerprint",
                      "pick_origin_fingerprint", "pick_selection_run_id",
                      "pick_research_id", "pick_sources", "pick_included")
            by_slot = {row[0]: dict(zip(fields, row)) for row in rows}
            cur.execute("""SELECT notification_slot,recorded_at,reason
                           FROM scheduled_notification_silent_slots
                           WHERE notification_slot>=?
                           ORDER BY notification_slot DESC LIMIT 56""", (cutoff,))
            for silent_slot, recorded_at, reason in cur.fetchall():
                if silent_slot not in by_slot:
                    by_slot[silent_slot] = {
                        "notification_slot": silent_slot,
                        "claimed_at": None, "attempted_at": None,
                        "delivered_at": None, "payload_hash": None,
                        "original_lines": None, "delivered_lines": None,
                        "category": "report", "http_status": None,
                        "error_code": None, "delivery_status": "silent",
                        "version": PICK_POLICY_VERSION,
                        "suppression_reason": reason, "suppressed_count": 0,
                        "part_count": None, "accepted_part_count": None,
                        "message_archive_status": None, "message_archive_id": None,
                        "pick_fingerprint": None,
                        "pick_origin_fingerprint": None,
                        "pick_selection_run_id": None,
                        "pick_research_id": None,
                        "pick_sources": None, "pick_included": False,
                    }
            for offset in range(days):
                date = current.date() - timedelta(days=offset)
                if date.weekday() >= 5:
                    continue
                for planned_time in ("10:15", "11:25", "14:35", "20:45"):
                    slot = f"{date.isoformat()}T{planned_time}+08:00"
                    if datetime.fromisoformat(slot) > current or slot in by_slot:
                        continue
                    by_slot[slot] = {
                        "notification_slot": slot, "claimed_at": None,
                        "attempted_at": None, "delivered_at": None,
                        "payload_hash": None, "original_lines": None,
                        "delivered_lines": None, "category": "report",
                        "http_status": None, "error_code": None,
                        "delivery_status": "unobserved",
                        "version": "pre-audit-unobserved",
                        "suppression_reason": "no_ledger_record",
                        "suppressed_count": 0,
                        "part_count": None, "accepted_part_count": None,
                        "message_archive_status": None, "message_archive_id": None,
                        "pick_fingerprint": None,
                        "pick_origin_fingerprint": None,
                        "pick_selection_run_id": None,
                        "pick_research_id": None,
                        "pick_sources": None, "pick_included": False,
                    }
            return [by_slot[slot] for slot in sorted(by_slot, reverse=True)[:56]]
        finally:
            conn.close()
