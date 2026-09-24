"""Best-effort archive gateway for local jobs and the protected scheduled CLI.

The caller must never log the request body or a transport exception: either may
contain private message text or credentials. The archive records only bounded,
sanitized outcome codes, never a destination URL or recipient address.
"""

from __future__ import annotations

import os
import logging
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import requests

log = logging.getLogger(__name__)


class ArchiveConflict(Exception):
    """The persisted idempotency identity disagrees with this payload."""


IN_ROUTER_DELIVERY = ContextVar("in_router_delivery", default=False)


def _remote_client():
    base = os.getenv("FOLIANT_AGENT_BASE_URL", "").strip().rstrip("/")
    token_path = os.getenv("FOLIANT_EXTERNAL_RESEARCH_TOKEN_FILE", "").strip()
    token = os.getenv("FOLIANT_EXTERNAL_RESEARCH_TOKEN", "").strip()
    if not base or not (token_path or token):
        return None
    parsed = urlsplit(base)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in
        {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("archive_transport_insecure")
    if not token:
        token = Path(token_path).read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("archive_token_missing")
    return base, token


def archive_action(action: str, payload: dict):
    """Return protected data. Raises an opaque error on any failure."""
    if action not in {"prepare", "start", "finish"}:
        raise ValueError("archive_action_invalid")
    remote = _remote_client()
    if remote:
        base, token = remote
        try:
            response = requests.post(
                base + "/api/machine/v1/agent/message-archive/" + action,
                json=payload, headers={"Authorization": "Bearer " + token},
                timeout=8,
            )
            if response.status_code == 409:
                raise ArchiveConflict("archive_identity_conflict")
            if response.status_code != 200:
                raise RuntimeError("archive_http_rejected")
            body = response.json()
            if body.get("status") != "complete" or not isinstance(body.get("data"), dict):
                raise RuntimeError("archive_result_invalid")
            return body["data"]
        except requests.RequestException:
            raise RuntimeError("archive_transport_unknown") from None
    from application.message_archive import MessageArchiveService
    service = MessageArchiveService()
    if action == "prepare":
        try:
            return service.prepare(**payload)
        except ValueError as exc:
            if str(exc) in {"message_archive_idempotency_conflict",
                            "message_archive_delivery_conflict"}:
                raise ArchiveConflict("archive_identity_conflict") from None
            raise
    if action == "start":
        return {"started": service.start(payload["delivery_id"])}
    return {"recorded": service.finish(**payload)}


def archived_call(*, channel: str, title: str, original_body: str,
                  final_body: str, sender, source: str, category: str = "report",
                  message_id: str | None = None, source_run_id: str | None = None,
                  idempotency_key: str | None = None,
                  business_as_of: str | None = None) -> bool:
    """Wrap one legacy direct send without changing its transport or route."""
    if IN_ROUTER_DELIVERY.get():
        direct_result = sender()
        return bool(direct_result.get("ok")) if isinstance(direct_result, dict) else bool(direct_result)
    delivery_id = None
    prepared = False
    try:
        row = archive_action("prepare", {
            "message_id": message_id or uuid4().hex, "source": source,
            "source_run_id": source_run_id, "category": category,
            "title": title, "original_body": original_body, "channel": channel,
            "final_body": final_body, "idempotency_key": idempotency_key,
            "business_as_of": business_as_of, "sensitivity": "private",
            "planned_at": None, "fallback_from": None, "retry_of": None,
        })
        if not row["should_send"]:
            return False
        delivery_id = row["delivery_id"]
        prepared = bool(archive_action("start", {"delivery_id": delivery_id})["started"])
        if not prepared:
            return False
    except ArchiveConflict:
        return False
    except Exception:
        if idempotency_key and category != "alert":
            return False
        log.warning("message archive unavailable before direct delivery; source=%s channel=%s",
                    source, channel)
    try:
        result = sender()
        if isinstance(result, dict):
            ok = bool(result.get("ok"))
            http_status = result.get("http_status")
            provider_code = result.get("provider_code")
            error_code = result.get("error_code")
            status = "accepted" if ok else "failed" if http_status else "unknown"
        else:
            ok = bool(result)
            http_status = None
            provider_code = "accepted" if ok else None
            error_code = None if ok else "transport_unknown"
            status = "accepted" if ok else "unknown"
    except Exception:
        ok = False
        http_status = None
        provider_code = None
        error_code = "transport_unknown"
        status = "unknown"
    if prepared and delivery_id:
        try:
            archive_action("finish", {"delivery_id": delivery_id,
                                     "status": status, "http_status": http_status,
                                     "provider_code": provider_code,
                                     "error_code": error_code})
        except Exception:
            log.warning("message archive finish unavailable; source=%s channel=%s",
                        source, channel)
    return ok
