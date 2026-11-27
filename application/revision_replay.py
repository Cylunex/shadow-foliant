"""Bounded hindsight impact replay; original manifests/results are never replaced."""
from data.reliability_store import ReliabilityStore
from application.results import payload_hash


def compare_rankings(before, after):
    left = {r["symbol"]: (i, r.get("total_score")) for i, r in enumerate(before, 1)}
    right = {r["symbol"]: (i, r.get("total_score")) for i, r in enumerate(after, 1)}
    return {"members_added": sorted(set(right) - set(left)), "members_removed": sorted(set(left) - set(right)),
            "rank_changed": sorted(s for s in left.keys() & right.keys() if left[s][0] != right[s][0]),
            "score_changed": sorted(s for s in left.keys() & right.keys() if left[s][1] != right[s][1])}


def _exclude_unchanged_work(repo, *, now):
    """Close legacy jobs whose decoded financial values never changed."""
    if repo.store._is_postgres:
        fields = "CAST(payload AS jsonb)->'impact'->'changed_fields' = CAST('[]' AS jsonb)"
    else:
        fields = "json_extract(payload,'$.impact.changed_fields') = '[]'"
    with repo.transaction() as cur:
        cur.execute(
            "UPDATE research_reliability_work SET state='excluded',updated_at=? "
            "WHERE work_id IN (SELECT work_id FROM research_reliability_work "
            f"WHERE kind='revision_replay' AND state='pending' AND {fields} "
            "ORDER BY work_id LIMIT 1600)",
            (now,),
        )
        return cur.rowcount


def replay_impact(store, *, impact, manifest_id):
    import json
    manifest = store.load_selection_manifest(manifest_id)
    if not manifest:
        raise ValueError("manifest_missing")
    if not store.financial_revision_in_manifest(manifest, impact):
        return {"status": "excluded", "reason": "source_revision_not_in_original_inputs", "manifest_id": manifest_id}
    corrected = store.replay_selection(manifest_id, financial_overrides={impact["table"]: {
        "symbol": impact["symbol"], "stat_date": impact["stat_date"], "values": impact["new_values"]}})
    if corrected["replay_status"] != "success":
        return {"status": "unavailable", "reason": "frozen_replay_incomplete", "manifest_id": manifest_id}
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT payload FROM selection_artifacts WHERE run_id=? AND artifact_type='formal_top15'", (manifest["run_id"],))
        old = cur.fetchone()
    finally:
        conn.close()
    if not old:
        return {"status": "unavailable", "reason": "original_formal_artifact_missing"}
    result = {"status": "complete", "run_id": manifest["run_id"], "manifest_id": manifest_id,
              "comparison": compare_rankings(json.loads(old[0]), corrected["formal_top15"]),
              "class": "hindsight_correction", "original_preserved": True, "recomputed_hashes": corrected["artifacts"]}
    return ReliabilityStore(store).once("revision_replay", payload_hash({"impact": impact, "manifest": manifest_id}), result)


def process_revision_impacts(store, *, now, limit=2):
    if type(limit) is not int or not 0 <= limit <= 2:
        raise ValueError("invalid_revision_replay_budget")
    repo = ReliabilityStore(store)
    if not limit:
        return {"completed": 0, "queue": repo.work_status("revision_replay"), "deferred": True}
    excluded = _exclude_unchanged_work(repo, now=now)
    # Existing work retains its identity. New impacts only queue matching facts.
    discovered = 0
    for impact in repo.list("revision_impact", limit=100):
        if not impact.get("new_values") or impact.get("changed_fields") == []:
            continue
        key = payload_hash(impact)
        if repo.get("revision_discovery", key):
            continue
        conn = store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT manifest_id FROM selection_input_manifests WHERE created_at<=?", (now,))
            ids = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        queued = 0
        for identity in ids:
            manifest = store.load_selection_manifest(identity)
            if manifest and store.financial_revision_in_manifest(manifest, impact):
                repo.enqueue("revision_replay", key + ":" + identity,
                             {"impact": impact, "manifest_id": identity}, priority=3)
                queued += 1
        repo.once("revision_discovery", key, {"manifest_count": len(ids), "queued_count": queued, "discovered_at": now})
        discovered += 1
        if discovered == 4:
            break
    completed, replays = excluded, 0
    for _ in range(160):
        rows = repo.claim("revision_replay", limit=1, now=now)
        if not rows:
            break
        work = rows[0]
        try:
            value = replay_impact(store, impact=work["impact"], manifest_id=work["manifest_id"])
            if value["status"] in {"complete", "excluded"}:
                repo.finish(work["work_id"], state=value["status"])
                completed += 1
            excluded += value["status"] == "excluded"
            replays += value["status"] != "excluded"
            if value["status"] == "unavailable":
                repo.once("revision_replay_error", f"{work['work_id']}:{work['attempt']}", value)
        except Exception as exc:
            repo.once("revision_replay_error", f"{work['work_id']}:{work['attempt']}", {"error_category": type(exc).__name__})
            replays += 1
        if replays >= limit:
            break
    return {
        "completed": completed, "excluded": excluded,
        "queue": repo.work_status("revision_replay"),
        "deferred": limit == 0,
    }
