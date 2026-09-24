"""Encrypted logical-message and per-channel delivery archive.

Only the protected service decrypts bodies. A transport acceptance is not proof
that the recipient's client displayed the message.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet


ARCHIVE_VERSION = "message-archive-v1"
DEFAULT_KEY_FILE = Path("/data/project/shadow-foliant-ops/secrets/message-archive.key")
SAFE_CODE = re.compile(r"^[a-zA-Z0-9._:-]{1,160}$")
CHANNELS = {"qq", "email", "dingtalk", "feishu", "wechat_work",
            "telegram", "discord", "slack", "webhook"}
STATUSES = {"accepted", "failed", "unknown"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key_from_file() -> bytes:
    path = Path(os.getenv("FOLIANT_MESSAGE_ARCHIVE_KEY_FILE") or DEFAULT_KEY_FILE)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("message_archive_key_permissions_invalid")
        key = os.read(descriptor, 256).strip()
    finally:
        os.close(descriptor)
    if len(key) != 44:
        raise ValueError("message_archive_key_invalid")
    return key


class MessageArchiveService:
    def __init__(self, store: Any = None, key: bytes | None = None,
                 clock: Any = None):
        if store is None:
            from data.research_store import ResearchStore
            store = ResearchStore(ensure_schema=False)
        self.store = store
        self.cipher = Fernet(key or _key_from_file())
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode("ascii")).decode("utf-8")

    @staticmethod
    def _refresh_status(cur: Any, message_id: str) -> None:
        cur.execute("""UPDATE notification_messages SET status=CASE
            WHEN NOT EXISTS(SELECT 1 FROM notification_deliveries d
                            WHERE d.message_id=notification_messages.message_id
                              AND d.status<>'accepted') THEN 'accepted'
            WHEN EXISTS(SELECT 1 FROM notification_deliveries d
                        WHERE d.message_id=notification_messages.message_id
                          AND d.status='accepted') THEN 'partial'
            WHEN EXISTS(SELECT 1 FROM notification_deliveries d
                        WHERE d.message_id=notification_messages.message_id
                          AND d.status IN ('sending','unknown')) THEN 'unknown'
            WHEN EXISTS(SELECT 1 FROM notification_deliveries d
                        WHERE d.message_id=notification_messages.message_id
                          AND d.status='pending') THEN 'prepared'
            ELSE 'failed' END WHERE message_id=?""", (message_id,))

    def prepare(self, *, message_id: str, source: str, source_run_id: str | None,
                category: str, title: str, original_body: str,
                channel: str, final_body: str, idempotency_key: str | None = None,
                business_as_of: str | None = None, sensitivity: str = "private",
                planned_at: str | None = None, fallback_from: str | None = None,
                retry_of: str | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", message_id or ""):
            raise ValueError("message_archive_id_invalid")
        if channel not in CHANNELS or sensitivity not in {"private", "sensitive"}:
            raise ValueError("message_archive_metadata_invalid")
        if not source or len(source) > 200 or not category or len(category) > 60:
            raise ValueError("message_archive_metadata_invalid")
        if idempotency_key and (len(idempotency_key) > 160
                                or not SAFE_CODE.fullmatch(idempotency_key)):
            raise ValueError("message_archive_idempotency_invalid")
        if any(len(value.encode("utf-8")) > 16 * 1024 * 1024
               for value in (title, original_body, final_body)):
            raise ValueError("message_archive_body_too_large")
        original_hash, final_hash = _digest(original_body), _digest(final_body)
        now = self.clock().isoformat(timespec="seconds")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""INSERT INTO notification_messages
                (message_id,source,source_run_id,category,title_cipher,original_cipher,
                 original_sha256,generated_at,business_as_of,idempotency_key,
                 sensitivity,version,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (message_id, source, source_run_id, category, self._encrypt(title),
                 self._encrypt(original_body), original_hash, now, business_as_of,
                 idempotency_key, sensitivity, ARCHIVE_VERSION, "prepared"))
            if idempotency_key:
                cur.execute("""SELECT message_id,original_sha256,category,title_cipher,source
                               FROM notification_messages WHERE idempotency_key=?""",
                            (idempotency_key,))
            else:
                cur.execute("""SELECT message_id,original_sha256,category,title_cipher,source
                               FROM notification_messages WHERE message_id=?""",
                            (message_id,))
            row = cur.fetchone()
            if (not row or row[1] != original_hash or row[2] != category
                    or self._decrypt(row[3]) != title or row[4] != source):
                raise ValueError("message_archive_idempotency_conflict")
            actual_message_id = str(row[0])
            delivery_id = uuid4().hex
            cur.execute("""INSERT INTO notification_deliveries
                (delivery_id,message_id,channel,target_label,final_body_cipher,
                 final_sha256,planned_at,status,version,fallback_from,retry_of)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(message_id,channel) DO NOTHING""",
                (delivery_id, actual_message_id, channel, channel,
                 self._encrypt(final_body), final_hash, planned_at, "pending",
                 ARCHIVE_VERSION, fallback_from, retry_of))
            cur.execute("""SELECT delivery_id,final_sha256,status,attempted_at,
                                  acknowledged_at
                           FROM notification_deliveries
                           WHERE message_id=? AND channel=?""",
                        (actual_message_id, channel))
            delivery = cur.fetchone()
            if not delivery or delivery[1] != final_hash:
                raise ValueError("message_archive_delivery_conflict")
            should_send = str(delivery[2]) == "pending" and delivery[3] is None
            suppression_reason = (None if should_send else
                                  "prior_accepted" if delivery[2] == "accepted" else
                                  "attempt_outcome_unknown" if delivery[2] in {"sending", "unknown"} else
                                  "prior_failed" if delivery[2] == "failed" else
                                  "prior_attempt")
            if not should_send:
                cur.execute("""UPDATE notification_deliveries
                               SET suppressed_count=suppressed_count+1,
                                   last_suppressed_at=?,suppression_reason=?
                               WHERE delivery_id=?""",
                            (now, suppression_reason, delivery[0]))
            self._refresh_status(cur, actual_message_id)
            conn.commit()
            status = str(delivery[2])
            return {"message_id": actual_message_id,
                    "delivery_id": str(delivery[0]), "status": status,
                    "should_send": should_send,
                    "suppression_reason": suppression_reason,
                    "attempted_at": delivery[3], "acknowledged_at": delivery[4]}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start(self, delivery_id: str) -> bool:
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""UPDATE notification_deliveries
                           SET status='sending',attempted_at=?
                           WHERE delivery_id=? AND status='pending' AND attempted_at IS NULL""",
                        (self.clock().isoformat(timespec="seconds"), delivery_id))
            started = int(cur.rowcount or 0) == 1
            conn.commit()
            return started
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finish(self, delivery_id: str, *, status: str,
               http_status: int | None = None, provider_code: str | None = None,
               error_code: str | None = None) -> bool:
        if status not in STATUSES:
            raise ValueError("message_archive_status_invalid")
        if http_status is not None and not 100 <= http_status <= 599:
            raise ValueError("message_archive_http_status_invalid")
        if any(value and not SAFE_CODE.fullmatch(value)
               for value in (provider_code, error_code)):
            raise ValueError("message_archive_result_invalid")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""UPDATE notification_deliveries
                           SET status=?,acknowledged_at=?,http_status=?,
                               provider_code=?,error_code=?
                           WHERE delivery_id=? AND status='sending'""",
                        (status, self.clock().isoformat(timespec="seconds")
                         if status != "unknown" else None,
                         http_status, provider_code, error_code, delivery_id))
            recorded = int(cur.rowcount or 0) == 1
            if recorded:
                cur.execute("SELECT message_id FROM notification_deliveries WHERE delivery_id=?",
                            (delivery_id,))
                self._refresh_status(cur, str(cur.fetchone()[0]))
            conn.commit()
            return recorded
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_messages(self, *, from_date: str | None = None,
                      to_date: str | None = None, offset: int = 0,
                      limit: int = 20) -> list[dict[str, Any]]:
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 50))
        lower = str(from_date or "0001-01-01")
        upper = str(to_date or "9999-12-31")
        if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", lower)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", upper)):
            raise ValueError("message_archive_date_invalid")
        if lower > upper:
            raise ValueError("message_archive_date_invalid")
        upper_exclusive = (datetime.fromisoformat(upper) + timedelta(days=1)).date().isoformat() if upper != "9999-12-31" else "9999-12-31T23:59:59Z"
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT message_id,source,source_run_id,category,
                                  original_sha256,generated_at,business_as_of,
                                  idempotency_key,sensitivity,version,status
                           FROM notification_messages
                           WHERE generated_at>=? AND generated_at<?
                           ORDER BY generated_at DESC,message_id DESC LIMIT ? OFFSET ?""",
                        (lower, upper_exclusive,
                         limit, offset))
            fields = ("message_id", "source", "source_run_id", "category",
                      "original_sha256", "generated_at", "business_as_of",
                      "idempotency_key", "sensitivity", "version", "status")
            return [dict(zip(fields, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def detail(self, message_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{32}", message_id or ""):
            raise ValueError("message_archive_id_invalid")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("""SELECT source,source_run_id,category,title_cipher,
                                  original_cipher,original_sha256,generated_at,
                                  business_as_of,idempotency_key,sensitivity,version,status
                           FROM notification_messages WHERE message_id=?""", (message_id,))
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("""SELECT delivery_id,channel,target_label,final_body_cipher,
                                  final_sha256,planned_at,attempted_at,acknowledged_at,
                                  status,http_status,provider_code,error_code,
                                  retry_of,fallback_from,version,
                                  suppressed_count,last_suppressed_at,suppression_reason
                           FROM notification_deliveries WHERE message_id=?
                           ORDER BY channel""", (message_id,))
            deliveries = []
            for item in cur.fetchall():
                deliveries.append({
                    "delivery_id": item[0], "channel": item[1],
                    "target_label": item[2], "final_body": self._decrypt(item[3]),
                    "final_sha256": item[4], "planned_at": item[5],
                    "attempted_at": item[6], "acknowledged_at": item[7],
                    "status": item[8], "http_status": item[9],
                    "provider_code": item[10], "error_code": item[11],
                    "retry_of": item[12], "fallback_from": item[13],
                    "version": item[14], "suppressed_count": item[15],
                    "last_suppressed_at": item[16],
                    "suppression_reason": item[17],
                })
            return {
                "message_id": message_id, "source": row[0],
                "source_run_id": row[1], "category": row[2],
                "title": self._decrypt(row[3]), "original_body": self._decrypt(row[4]),
                "original_sha256": row[5], "generated_at": row[6],
                "business_as_of": row[7], "idempotency_key": row[8],
                "sensitivity": row[9], "version": row[10],
                "status": row[11], "deliveries": deliveries,
            }
        finally:
            conn.close()
