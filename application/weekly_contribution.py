"""Read-only weekly comparison of public, frozen research batches."""
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
from math import isfinite
from statistics import mean

from application.market_prices import SHANGHAI, shanghai_instant
from application.weekly_paper_pair import KIND, evaluate_pair

FORMAL = "formal_local_fusion_top15"
BLIND = "losna_blind"
HORIZON = 5


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def _prices(symbols, labels, signal_day, earliest, cutoff, now, *, paper_id=None):
    """All members need one stored observation of the same vintage and interval."""
    values, bases = [], set()
    for symbol in symbols:
        rows = labels.get(symbol, [])
        if paper_id:
            rows = [r for r in rows if r["evidence"].get("research_id") == paper_id]
        if len(rows) != 1:
            continue
        r = rows[0]
        evaluated = shanghai_instant(r["evaluated_at"])
        if (not evaluated or evaluated > now or r["state"] != "matured" or not r["metric"] or r["value"] is None
                or not isfinite(r["value"]) or not r["entry"] or not r["exit"]
                or r["entry"] < earliest or not signal_day < r["entry"] <= r["exit"] <= cutoff):
            continue
        values.append(r["value"])
        bases.add((r["entry"], r["exit"], r["metric"]))
    comparable = len(values) == len(symbols) and len(bases) == 1
    return {"total": len(symbols), "matured": len(values),
            "mean_pct": round(mean(values), 4) if comparable else None,
            "basis": next(iter(bases)) if comparable else None}


def _batch(run_id, day, capsule, records, labels, preparation, sessions, now):
    received = [r for r in records if r["status"] == "received"]
    row = {"run_id": run_id, "date": day, "packets": len(records),
           "preparation": preparation, "reviews": [], "difference_pp": None}
    if not capsule:
        return {**row, "state": "missing_capsule"}
    if not records:
        return {**row, "state": "missing_input" if preparation in ("data_missing", "data_unavailable") else "no_packet"}
    if len(received) > 1 or not received and len(records) > 1:
        return {**row, "state": "multiple_versions"}
    record = (received or records)[0]
    packet = record["packet"]
    pool = {str(r["symbol"]) for r in packet["candidates"]}
    facts = {r["evidence_id"]: r["fact"] for r in packet["evidence"]}
    scope = next((v for v in facts.values() if "prefilter" in v and "manifest_id" in v), {})
    formal = [str(r["symbol"]) for r in capsule.get("opportunity_set", {}).get("top15", [])]
    control = list(dict.fromkeys(s for s in formal if s in pool))
    row.update(pool_count=len(pool), formal_total=len(formal), control_symbols=control,
               manifest_id=scope.get("manifest_id"), packet_id=record["object_id"],
               producer_version=packet.get("schema_version", "losna-research-v1"))
    if not scope.get("manifest_id") or scope["manifest_id"] != capsule.get("manifest_id"):
        return {**row, "state": "manifest_mismatch"}
    if not received:
        deadline = shanghai_instant(packet.get("deadline"))
        return {**row, "state": "expired" if deadline and deadline <= now else record["status"]}
    locked = shanghai_instant(record.get("received_at"))
    if locked and locked > now:
        return {**row, "state": "pending"}
    seen = []
    for review in record["result"]["reviews"]:
        symbol = review["symbol"]
        seen.append(symbol)
        row["reviews"].append({"symbol": symbol, "in_formal": symbol in control,
            "reason": review["reason"], "counterevidence": review["counterevidence"],
            "evidence": [{"id": ref, "fact": facts[ref]} for ref in dict.fromkeys(review["evidence_refs"]) if ref in facts]})
    row["next_check"] = record["result"]["next_check"]
    if not seen:
        return {**row, "state": "abstained"}
    # Receipt time matters when an old capsule is queued after its entry open.
    opened = shanghai_instant(capsule.get("earliest_execution_at"))
    if not opened or not locked or locked >= opened or capsule.get("recording_mode") != "contemporaneous":
        return {**row, "state": "late_or_backfilled"}
    last_closed = now.date() if now.time() >= time(15) else now.date() - timedelta(days=1)
    blind = _prices(seen, labels[BLIND], day, opened.date().isoformat(), last_closed.isoformat(), now, paper_id=record["object_id"])
    official = _prices(control, labels[FORMAL], day, opened.date().isoformat(), last_closed.isoformat(), now)
    row.update(blind=blind, formal=official)
    if not control:
        return {**row, "state": "empty_control"}
    if blind["basis"] and blind["basis"] == official["basis"]:
        return {**row, "state": "paired", "difference_pp": round(blind["mean_pct"] - official["mean_pct"], 4)}
    if blind["basis"] and official["basis"]:
        return {**row, "state": "different_basis"}
    elapsed = sum(day < d <= last_closed.isoformat() for d in sessions)
    return {**row, "state": "awaiting_window" if elapsed < HORIZON else "awaiting_labels"}


def weekly_contribution(store, *, week_of=None, now=None):
    now = shanghai_instant(now) if now is not None else datetime.now(SHANGHAI)
    chosen = date.fromisoformat(week_of) if week_of else now.date()
    start = chosen - timedelta(days=chosen.weekday())
    if start > now.date():
        raise ValueError("future_week")
    end = min(start + timedelta(days=6), now.date())
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT r.run_id,r.selection_date,a.payload FROM selection_runs r
            LEFT JOIN selection_artifacts a ON a.run_id=CAST(r.run_id AS TEXT) AND a.artifact_type='decision_capsule'
            WHERE r.publication_status='published' AND r.selection_date BETWEEN ? AND ?
            ORDER BY r.selection_date,r.run_id""", (start.isoformat(), end.isoformat()))
        runs = {str(r): (str(d), _decode(c)) for r, d, c in cur.fetchall()}
        batches = []
        if runs:
            args = tuple(runs)
            slots = ",".join("?" for _ in args)
            packet_run = ("CAST(r.payload AS jsonb)->'packet'->>'run_id'" if store._is_postgres
                          else "CAST(json_extract(r.payload,'$.packet.run_id') AS TEXT)")
            cur.execute(f"""SELECT r.payload FROM research_reliability_records r
                WHERE r.kind='losna_research' AND r.owner_id='research' AND {packet_run} IN ({slots})
                AND NOT EXISTS (SELECT 1 FROM research_reliability_records n WHERE n.kind=r.kind AND
                    n.owner_id=r.owner_id AND n.object_id=r.object_id AND n.revision>r.revision)""", args)
            packets = defaultdict(list)
            for raw, in cur.fetchall():
                record = _decode(raw)
                if record["packet"]["purpose"] == "independent_discovery":
                    packets[str(record["packet"]["run_id"])].append(record)
            cur.execute(f"""SELECT object_id,payload FROM research_reliability_records
                WHERE owner_id='research' AND kind='losna_preparation' AND object_id IN ({slots})
                ORDER BY revision""", args)
            prepared = {r: _decode(p).get("discovery_status") for r, p in cur.fetchall()}
            cur.execute(f"""SELECT n.selection_run_id,n.strategy_id,n.symbol,n.evidence,o.outcome_status,
                o.entry_date,o.exit_date,o.return_pct,o.metric_version,o.evaluated_at FROM selection_candidate_nominations n
                LEFT JOIN selection_candidate_outcomes o ON o.nomination_id=n.nomination_id AND o.horizon_days=?
                WHERE n.selection_run_id IN ({slots}) AND n.strategy_id IN (?,?) AND n.eligibility='eligible'""",
                        (HORIZON, *args, BLIND, FORMAL))
            labels = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
            for r, strategy, symbol, evidence, state, entry, exit_day, value, metric, evaluated_at in cur.fetchall():
                labels[str(r)][strategy][symbol].append(dict(evidence=_decode(evidence), state=state,
                    entry=entry, exit=exit_day, value=value, metric=metric, evaluated_at=evaluated_at))
            cur.execute("SELECT trade_date FROM research_trade_calendar WHERE trade_date>? AND trade_date<=? ORDER BY trade_date LIMIT 16",
                        (start.isoformat(), now.date().isoformat()))
            sessions = [str(r[0]) for r in cur.fetchall()]
            for run, (day, capsule) in runs.items():
                batches.append(_batch(run, day, capsule, packets[run], labels[run], prepared.get(run), sessions, now))
            cur.execute(f"""SELECT object_id,payload FROM research_reliability_records WHERE owner_id='research'
                AND kind=? AND object_id IN ({slots}) AND revision=1""", (KIND, *args))
            targets = {r: _decode(p) for r, p in cur.fetchall()}
            fact_keys = sorted({t["entry_at"][:10] + ":" + s for t in targets.values() for s in t["blind"] + t["formal"]})
            facts = {}
            if fact_keys:
                as_of_utc = now.astimezone(timezone.utc).isoformat()
                placeholders = ",".join("?" for _ in fact_keys)
                cur.execute(f"""SELECT r.object_id,r.payload FROM research_reliability_records r
                    WHERE r.owner_id='research' AND r.kind='execution_fact' AND r.created_at<=? AND r.object_id IN ({placeholders})
                    AND NOT EXISTS (SELECT 1 FROM research_reliability_records n WHERE n.owner_id=r.owner_id
                        AND n.kind=r.kind AND n.object_id=r.object_id AND n.revision>r.revision AND n.created_at<=?)""",
                            (as_of_utc, *fact_keys, as_of_utc))
                facts = {key: _decode(raw) for key, raw in cur.fetchall()}
            for batch in batches:
                target = targets.get(batch["run_id"])
                if target and shanghai_instant(target["recorded_at"]) <= now:
                    prefix = target["entry_at"][:10] + ":"
                    batch["paper_pair"] = evaluate_pair(target, {k[len(prefix):]: v for k, v in facts.items() if k.startswith(prefix)}, now)
        # Whole-account marks are context only. Never impute a paired net advantage.
        cur.execute("SELECT payload,updated_at FROM research_model_portfolios WHERE baseline='source:formal'")
        book = cur.fetchone()
        available = book and shanghai_instant(book[1]) and shanghai_instant(book[1]) <= now
        closed = now.date() if now.time() >= time(15) else now.date() - timedelta(days=1)
        marks = [m for m in (_decode(book[0]).get("marks", []) if available else [])
                 if start.isoformat() <= m["trade_date"] <= min(end, closed).isoformat()]
        mark = max(marks, key=lambda m: m["trade_date"]) if marks else None
    finally:
        conn.close()
    reviews = [r for b in batches for r in b["reviews"]]
    summary = {"nominated": len(reviews), "outside_formal": sum(not r["in_formal"] for r in reviews),
               "with_source": sum(bool(r["evidence"]) for r in reviews),
               "price_pairs": sum(b["difference_pp"] is not None for b in batches),
               "net_pairs": sum((b.get("paper_pair") or {}).get("status") == "marked" for b in batches)}
    return {"summary": summary, "start": start.isoformat(), "end": end.isoformat(), "as_of": now.isoformat(timespec="seconds"),
            "horizon_days": HORIZON, "batches": batches, "counts": dict(Counter(b["state"] for b in batches)),
            "formal_model": {k: mark.get(k) for k in ("trade_date","status","net_return_pct","fees_paid")} if mark else None}
