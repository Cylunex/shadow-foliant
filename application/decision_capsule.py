"""Frozen decision facts versus scoped dynamic context. No network I/O."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta
from application.results import payload_hash


def build_capsule(*, run_id, metadata, top15, top5, published_at, next_open_date=None):
    publication = datetime.fromisoformat(published_at)
    if publication.tzinfo is None:
        raise ValueError("publication_timezone_required")
    selection_date = str(metadata.get("selection_date") or (metadata.get("decision_context") or {}).get("selection_date") or published_at[:10])
    earliest = f"{next_open_date}T09:30:00+08:00" if next_open_date else None
    if earliest and datetime.fromisoformat(earliest) <= publication:
        raise ValueError("execution_precedes_publication")
    core = {"schema_version": "decision-capsule-v1", "run_id": run_id,
            "selection_date": selection_date, "market_as_of": metadata.get("market_as_of"),
            "decision_at": metadata.get("decision_at") or published_at,
            "data_cutoff_at": (metadata.get("decision_context") or {}).get("decision_at"),
            "published_at": published_at, "earliest_execution_at": earliest,
            "recording_mode": "contemporaneous" if selection_date == published_at[:10] else "backfilled",
            "manifest_id": metadata.get("manifest_id"), "policy_hash": metadata.get("policy_hash"),
            "code_revision": (metadata.get("decision_context") or {}).get("code_revision"),
            "opportunity_set": {"top15": deepcopy(top15), "top5": deepcopy(top5)},
            "execution_model": "next-open-raw-v1", "scope": "research"}
    core["capsule_id"] = "dc_" + payload_hash(core)
    return core


def context_envelope(capsule, *, now, quotes=None, holdings_version=None,
                     authorized_portfolio=False):
    if holdings_version is not None and not authorized_portfolio:
        raise PermissionError("portfolio_scope_required")
    return {"frozen": deepcopy(capsule), "dynamic": {
        "observed_at": now, "quotes": quotes or {}, "holdings_version": holdings_version,
        "scope": "portfolio" if authorized_portfolio else "research"}}


def context_diff(before, after):
    keys = ("manifest_id", "policy_hash", "code_revision", "published_at", "market_as_of")
    return {key: {"before": before.get(key), "after": after.get(key)}
            for key in keys if before.get(key) != after.get(key)}
