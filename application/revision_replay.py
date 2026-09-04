"""Bounded hindsight impact replay; original manifests/results are never replaced."""
from data.reliability_store import ReliabilityStore
from application.results import payload_hash


def compare_rankings(before, after):
    left = {r["symbol"]: (i, r.get("total_score")) for i, r in enumerate(before, 1)}
    right = {r["symbol"]: (i, r.get("total_score")) for i, r in enumerate(after, 1)}
    return {"members_added": sorted(set(right) - set(left)), "members_removed": sorted(set(left) - set(right)),
            "rank_changed": sorted(s for s in left.keys() & right.keys() if left[s][0] != right[s][0]),
            "score_changed": sorted(s for s in left.keys() & right.keys() if left[s][1] != right[s][1])}


def replay_impact(store, *, impact, manifest_id):
    import json
    manifest = store.load_selection_manifest(manifest_id)
    if not manifest:
        raise ValueError("manifest_missing")
    frames = store.load_financial_facts_from_manifest(manifest_id)
    frame = frames.get(impact["table"])
    if frame is None or frame.empty or impact["symbol"] not in set(frame["symbol"].astype(str)):
        return {"status": "excluded", "reason": "field_not_consumed", "manifest_id": manifest_id}
    period = "stat_date" if "stat_date" in frame else "statDate"
    if period not in frame or not (frame["symbol"].astype(str).eq(impact["symbol"]) &
                                  frame[period].astype(str).eq(impact["stat_date"])).any():
        return {"status": "excluded", "reason": "period_not_consumed", "manifest_id": manifest_id}
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


def process_revision_impacts(store, *, now):
    repo = ReliabilityStore(store)
    # Discovery is database-only; queue stores all discovered manifest IDs while
    # only two expensive replays may run per invocation. No external fetches.
    for impact in repo.list("revision_impact", limit=100):
        if not impact.get("new_values"):
            continue
        conn = store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT manifest_id FROM selection_input_manifests WHERE created_at<=?", (now,))
            ids = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        key = payload_hash(impact)
        if repo.get("revision_discovery", key):
            continue
        for identity in ids:
            repo.enqueue("revision_replay", key + ":" + identity, {"impact": impact, "manifest_id": identity}, priority=3)
        repo.once("revision_discovery", key, {"manifest_count": len(ids), "discovered_at": now})
    completed = 0
    for work in repo.claim("revision_replay", limit=2, now=now):
        try:
            value = replay_impact(store, impact=work["impact"], manifest_id=work["manifest_id"])
            if value["status"] in {"complete", "excluded"}:
                repo.finish(work["work_id"], state=value["status"])
                completed += 1
        except Exception as exc:
            repo.once("revision_replay_error", f"{work['work_id']}:{work['attempt']}", {"error_category": type(exc).__name__})
    return {"completed": completed, "queue": repo.work_status("revision_replay")}
