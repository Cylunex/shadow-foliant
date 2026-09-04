"""Payload-free admission telemetry; raw retention is a separate explicit opt-in.

An SDK boundary receipt is not a count of invisible internal HTTP retries. The
bounded process queue may drop telemetry on saturation/crash, reported explicitly.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from queue import Queue, Empty, Full
import threading
import time
import uuid

_queue = Queue(maxsize=2048)
_counts = Counter()
_lock = threading.Lock()
FAMILIES = {"eastmoney_saas": "eastmoney", "easy_tdx": "tdx", "mootdx": "tdx",
            "tdx_python": "tdx", "eltdx": "tdx", "pywencai": "ths"}


def source_family(provider, upstream=None):
    # An unspecified AkShare upstream is unknown, never an independent vote.
    return upstream or FAMILIES.get(provider, "unknown" if provider == "akshare" else provider)


def receipt(provider, endpoint, started, *, error=None, status=None, **metadata):
    value = {"object_id": uuid.uuid4().hex, "provider": provider, "endpoint": endpoint,
             "source_family": source_family(provider), "observed_at": datetime.now(timezone.utc).isoformat(),
             "elapsed_ms": round((time.monotonic() - started) * 1000),
             "status": status or ("FAILED" if error else "SUCCESS"),
             "error_category": type(error).__name__ if error else None,
             "observation_level": "sdk_admission", "http_attempts": None,
             "result_coverage": "unknown", "raw_replayable": False, **metadata}
    try:
        _queue.put_nowait(value)
    except Full:
        with _lock:
            _counts["dropped"] += 1


def flush(store, limit=256):
    from data.reliability_store import ReliabilityStore
    repo = ReliabilityStore(store)
    count = 0
    for _ in range(limit):
        try:
            value = _queue.get_nowait()
        except Empty:
            break
        try:
            repo.once("acquisition", value["object_id"], value)
            count += 1
        except Exception:
            _queue.put_nowait(value)
            raise
    return {"persisted": count, "pending": _queue.qsize(), "dropped": _counts["dropped"],
            "scope": "instrumented process boundaries; crash loss possible"}


def archive_raw(content, *, root, retention_allowed=False, max_bytes=1048576):
    if not retention_allowed:
        return {"raw_replayable": False, "reason": "retention_not_authorized"}
    if not isinstance(content, bytes) or len(content) > max_bytes:
        raise ValueError("raw_archive_size_or_type")
    directory = Path(root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("raw_archive_requires_private_directory")
    digest = hashlib.sha256(content).hexdigest()
    path = directory / digest
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("raw_archive_integrity_failure")
    else:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
    return {"raw_replayable": True, "raw_digest": digest, "locator": digest}


def compatible_fact(left, right):
    keys = ("symbol", "field", "unit", "currency", "period", "basis", "effective_date")
    return all(left.get(k) is not None and left.get(k) == right.get(k) for k in keys)


def fact_observation(*, symbol, field, value, period, published_at, first_seen_at,
                     provider, revision, unit="unknown", currency="unknown", basis="unknown"):
    from application.results import payload_hash
    observation = {"symbol": symbol, "field": field, "value": value, "period": period,
                   "published_at": published_at, "first_seen_at": first_seen_at,
                   "provider": provider, "source_family": source_family(provider), "source_revision": revision,
                   "unit": unit, "currency": currency, "basis": basis,
                   "parser_version": "financial-cell-v1", "granularity": "field",
                   "strict_semantics": "unknown" not in {unit, currency, basis},
                   "history_class": "observed_at_first_seen_not_backdated"}
    observation["object_id"] = payload_hash({k: v for k, v in observation.items() if k != "first_seen_at"})
    return observation


def observed_http_body(provider, body, *, started):
    """Only explicitly licensed/public source payloads may be retained on disk."""
    allowed = set(os.getenv("RESEARCH_RAW_RETAIN_PROVIDERS", "").split(",")) & {"tencent", "sina", "cninfo", "cls", "eastmoney"}
    retained = {"raw_replayable": False}
    if provider in allowed and os.getenv("RESEARCH_RAW_ARCHIVE_DIR"):
        try:
            retained = archive_raw(body, root=os.environ["RESEARCH_RAW_ARCHIVE_DIR"], retention_allowed=True)
        except (ValueError, OSError):
            retained = {"raw_replayable": False, "archive_status": "unavailable"}
    receipt(provider, "http_response", started, observation_level="http_response", http_attempts=1,
            response_bytes=len(body), **retained)


def revision_impact(old, new, dependencies):
    changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    return {"changed_fields": changed, "affected": [d for d in dependencies
            if set(d.get("fields", [])) & set(changed) or d.get("granularity") == "dataset"],
            "requires_new_revision": bool(changed), "original_preserved": True}
