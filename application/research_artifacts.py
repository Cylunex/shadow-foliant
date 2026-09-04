"""Versioned research-artifact contracts built from deterministic evidence metadata."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence
import json

from application.results import clean_json, now_iso, payload_hash


SCHEMA_VERSION = "research-artifact-v1"


def _first(mapping: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, "", []):
            return value
    return default


def _list(value: Any, *, limit: int = 12) -> list[Any]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [clean_json(item) for item in values[:limit]]


def _freshness(as_of: Any, decision_at: Any) -> str:
    try:
        left = date.fromisoformat(str(as_of)[:10])
        right = date.fromisoformat(str(decision_at)[:10])
        age = max(0, (right - left).days)
        return "current" if age <= 3 else ("aging" if age <= 30 else "stale")
    except Exception:
        return "unknown"


def evidence_from_facts(facts: Mapping[str, Any] | None, *, decision_at: str,
                        default_as_of: str | None = None) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for name, raw in (facts or {}).items():
        if name.startswith("_"):
            continue
        value = raw if isinstance(raw, Mapping) else {}
        provenance = value.get("provenance") if isinstance(value.get("provenance"), Mapping) else {}
        as_of = _first(value, ("as_of", "market_as_of", "trade_date", "date"), default_as_of)
        dataset_id = _first(value, ("dataset_id", "snapshot_id", "revision_set_id"))
        quality = value.get("data_quality") if isinstance(value.get("data_quality"), Mapping) else {}
        evidence.append({
            "evidence_id": payload_hash({"group": name, "dataset_id": dataset_id, "as_of": as_of}),
            "fact_group": str(name)[:80],
            "source_id": str(_first(provenance, ("provider", "source"), name))[:80],
            "dataset_id": str(dataset_id)[:160] if dataset_id else None,
            "as_of": str(as_of)[:40] if as_of else None,
            "retrieved_at": str(_first(provenance, ("retrieved_at",), ""))[:40] or None,
            "freshness": _freshness(as_of, decision_at),
            "quality": str(_first(quality, ("level", "status"),
                                  _first(value, ("quality_status",), "unknown")))[:40],
        })
    return evidence[:40]


def build_research_artifact(*, subject: str, run_id: str, facts: Mapping[str, Any] | None,
                            provenance: Mapping[str, Any], data_quality: Mapping[str, Any] | None,
                            analysis: Mapping[str, Any] | None = None,
                            formal: bool = False, artifact_kind: str = "security-research",
                            evidence: list[dict[str, Any]] | None = None,
                            created_at: str | None = None) -> dict[str, Any]:
    decision_at = str(provenance.get("decision_at") or now_iso())
    analysis = analysis if isinstance(analysis, Mapping) else {}
    thesis = {
        "direction": _first(analysis, ("rating", "direction", "action"), "unavailable"),
        "confidence": _first(analysis, ("confidence", "confidence_level"),
                             (data_quality or {}).get("level", "unknown")),
        "horizon": _first(analysis, ("horizon", "period", "holding_period"), "unspecified"),
        "reasons": _list(_first(analysis, ("reasons", "key_points", "rationale"))),
        "risks": _list(_first(analysis, ("risks", "risk_factors", "warnings"))),
        "action": _first(analysis, ("action", "recommendation", "rating"), "observe"),
    }
    invalidation = _list(_first(
        analysis, ("invalidation_conditions", "invalidations", "stop_conditions", "stop_loss")
    ))
    if not invalidation:
        invalidation = ["New material evidence or a newer formal dataset publication requires review."]
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": str(artifact_kind),
        "subject": str(subject),
        "run_id": str(run_id),
        "formal": bool(formal),
        "decision_context": clean_json(dict(provenance)),
        "thesis": clean_json(thesis),
        "evidence": clean_json(evidence if evidence is not None else evidence_from_facts(
            facts, decision_at=decision_at, default_as_of=provenance.get("market_as_of")
        )),
        "invalidation_conditions": invalidation,
        "next_actions": _list(_first(analysis, ("next_actions", "follow_up"), ["Refresh evidence before acting."])),
        "data_quality": clean_json(dict(data_quality or {})),
        "provenance": clean_json(dict(provenance)),
        "created_at": str(created_at or now_iso()),
    }
    artifact["artifact_id"] = "ra_" + payload_hash(artifact)
    artifact["payload_hash"] = payload_hash(artifact)
    return artifact


def build_selection_research_artifact(*, run_id: str, selection_date: str,
                                      candidates: Sequence[Mapping[str, Any]],
                                      metadata: Mapping[str, Any],
                                      input_manifest: Mapping[str, Any]) -> dict[str, Any]:
    context = dict(metadata.get("decision_context") or {})
    provenance = {
        "run_id": run_id,
        "decision_at": metadata.get("decision_at") or context.get("decision_at"),
        "data_cutoff_at": context.get("decision_at"),
        "selection_date": selection_date,
        "published_at": metadata.get("published_at"),
        "market_as_of": metadata.get("market_as_of"),
        "financial_cutoff_at": context.get("financial_cutoff_at"),
        "universe_snapshot_id": metadata.get("universe_snapshot_id"),
        "input_manifest_id": metadata.get("manifest_id"),
        "policy_hash": metadata.get("policy_hash"),
        "code_revision": context.get("code_revision"),
    }
    evidence = [{
        "evidence_id": payload_hash({"run_id": run_id, "symbol": row.get("symbol"), "rank": rank}),
        "fact_group": "formal-selection-candidate",
        "source_id": "local-pit",
        "dataset_id": metadata.get("manifest_id"),
        "as_of": metadata.get("market_as_of"),
        "retrieved_at": None,
        "freshness": "current",
        "quality": "formal",
        "symbol": row.get("symbol"),
        "rank": rank,
    } for rank, row in enumerate(candidates, 1)]
    return build_research_artifact(
        subject=f"a-share-selection:{selection_date}",
        run_id=run_id,
        facts={},
        provenance=provenance,
        data_quality={
            "coverage": metadata.get("usable_qfq_coverage"),
            "financial_coverage": metadata.get("financial_coverage"),
            "publication_generations": input_manifest.get("publication_generations") or {},
        },
        analysis={
            "direction": "ranked-candidates",
            "confidence": "policy-validated",
            "horizon": "selection-policy",
            "reasons": ["Deterministic local PIT selection and diversification policy."],
            "risks": ["Ranking becomes stale when a required dataset advances."],
            "action": "review-candidates",
            "invalidation_conditions": [
                "Any required dataset generation changes.",
                "A formal readiness gate becomes incomplete.",
            ],
        },
        formal=True,
        artifact_kind="selection-research",
        evidence=evidence,
        created_at=str(provenance.get("published_at") or provenance.get("decision_at") or now_iso()),
    )


def latest_research_artifact(subject: str = "", *, formal_only: bool = True) -> dict[str, Any] | None:
    """Read one safe structured artifact; annotations remain a separate resource."""
    from db_compat import connect

    conn = connect("research_artifacts")
    try:
        cur = conn.cursor()
        clauses = []
        params: list[Any] = []
        if subject:
            clauses.append("subject=?")
            params.append(str(subject))
        if formal_only:
            clauses.append("formal=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        cur.execute(
            "SELECT payload FROM research_artifacts" + where
            + " ORDER BY created_at DESC LIMIT 1",
            tuple(params),
        )
        row = cur.fetchone()
        if not row:
            return None
        payload = row[0]
        return clean_json(payload if isinstance(payload, dict) else json.loads(payload or "{}"))
    finally:
        conn.close()


def append_ai_annotation(*, artifact_id: str, annotation_kind: str,
                         payload: Mapping[str, Any], connect_fn=None) -> dict[str, Any]:
    """Append a non-authoritative AI overlay without updating the formal artifact row."""
    if not str(artifact_id or "").strip():
        raise ValueError("artifact_id is required")
    if not str(annotation_kind or "").strip():
        raise ValueError("annotation_kind is required")
    if connect_fn is None:
        from db_compat import connect as connect_fn

    created_at = now_iso()
    safe_payload = {
        "authoritative": False,
        "artifact_id": str(artifact_id),
        "annotation_kind": str(annotation_kind)[:80],
        "content": clean_json(dict(payload)),
        "protected_fields": ["evidence", "thesis", "score", "membership", "order"],
        "created_at": created_at,
    }
    digest = payload_hash(safe_payload)
    annotation_id = "raa_" + payload_hash({
        "artifact_id": artifact_id, "annotation_kind": annotation_kind,
        "payload_hash": digest,
    })
    conn = connect_fn("research_artifacts")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT payload_hash FROM research_artifacts WHERE artifact_id=? AND formal=1",
            (str(artifact_id),),
        )
        if not cur.fetchone():
            raise ValueError("formal research artifact was not found")
        cur.execute(
            """INSERT INTO research_artifact_annotations
               (annotation_id,artifact_id,annotation_kind,payload_hash,payload,created_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT(annotation_id) DO NOTHING""",
            (annotation_id, str(artifact_id), str(annotation_kind)[:80], digest,
             json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")), created_at),
        )
        conn.commit()
        return {"annotation_id": annotation_id, **safe_payload, "payload_hash": digest}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
